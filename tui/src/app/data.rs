//! Async fetch/parse free-function layer extracted from `app/mod.rs` (#743).
//!
//! Network I/O, SQLite reads, subprocess spawns, parse helpers.  No quadraui
//! rendering types appear here.
use std::net::{TcpStream, ToSocketAddrs};
use std::path::PathBuf;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use rusqlite::{Connection, OpenFlags};
use super::types::*;

/// Messages sent from the background SSE watch thread to the main thread.
pub(crate) enum SseWatchMsg {
    /// New log text arrived; `last_id` is the byte-offset after this chunk
    /// (used as `Last-Event-Id` on reconnect to resume without refetching).
    Lines { last_id: u64, text: String },
    /// Stream ended cleanly (agent sent `event: end`). No reconnect needed.
    Done { last_id: u64 },
    /// Connection or read error. The main thread decides whether to reconnect.
    Error(String),
    /// SSE keepalive comment received. Used to detect when the receiver has
    /// been dropped (cancel signal): if `tx.send` fails, the thread exits.
    Heartbeat,
}

/// Maximum number of concurrent SSE watch sessions held in `CoordApp.watch_pool`.
/// When adding a new session would exceed this limit the least-recently-focused
/// entry is evicted (dropping its `Receiver` cancels the background thread).
pub(crate) const WATCH_POOL_CAP: usize = 8;

/// State for the live SSE log-stream connection backing the watch overlay.
///
/// Held inside `WatchContext` in the `watch_pool` map.  Dropped (and thus the
/// background thread cancelled) when the context is evicted from the pool.
pub(crate) struct WatchSseState {
    /// Receive end of the channel from the background SSE thread.
    pub(crate) rx: std::sync::mpsc::Receiver<SseWatchMsg>,
    /// Accumulated raw log lines, appended as `Lines` messages arrive.
    pub(crate) lines: Vec<String>,
    /// Wall-clock arrival time for each entry in `lines` (parallel vec).
    /// Used to compute per-turn elapsed time in the watch overlay.
    pub(crate) line_times: Vec<Instant>,
    /// Count of `"type":"assistant"` events seen so far — drives the
    /// live turn-count badge on the Active stage box.
    pub(crate) current_turn: usize,
    /// Byte offset of the last received event, for `Last-Event-Id` on reconnect.
    pub(crate) last_event_id: u64,
    /// Number of connection failures in the current 10-second window.
    pub(crate) fail_count: u32,
    /// When the first failure in the current window occurred, for TTL reset.
    pub(crate) first_fail_at: Option<Instant>,
    /// True once a clean `end` event arrives or the failure limit is hit.
    /// When true, no further reconnect attempts are made.
    pub(crate) done: bool,
    /// Machine hostname, stored here so reconnect doesn't need to look up the
    /// machine list again.
    pub(crate) host: String,
    /// Partial trailing line carried over between SSE chunks. The agent reads
    /// the log in fixed 4 KB chunks (events.LOG_CHUNK_SIZE), so a long JSON
    /// line (e.g. a `{"type":"result"...}` event with the full review body)
    /// can be split mid-line. Without reassembly the client would parse two
    /// broken halves and lose `total_cost_usd` / `stop_reason` from the
    /// metrics line. Held here until the next chunk arrives.
    pub(crate) pending_tail: String,
}

/// Parsed fields from a successful `/health` HTTP response.
pub(crate) struct MachineHealthResult {
    pub(crate) version: String,
    pub(crate) worktree_bytes: u64,
}

/// Spawn a background thread that fetches `/health` from a remote agent and
/// parses the version + worktree_bytes fields.  Returns a `Receiver` that
/// yields `Ok(result)` or `Err(error_string)`.
pub(crate) fn spawn_machine_health(
    host: &str,
    port: u16,
) -> std::sync::mpsc::Receiver<Result<MachineHealthResult, String>> {
    let (tx, rx) = std::sync::mpsc::channel();
    let url = format!("http://{}:{}/health", host, port);
    std::thread::spawn(move || {
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(std::time::Duration::from_secs(2))
            .timeout(std::time::Duration::from_secs(2))
            .build();
        let result = match agent.get(&url).call() {
            Ok(resp) => match resp.into_string() {
                Ok(body) => match serde_json::from_str::<serde_json::Value>(&body) {
                    Ok(v) => {
                        let version = v
                            .get("version")
                            .and_then(|x| x.as_str())
                            .unwrap_or("?")
                            .to_string();
                        let worktree_bytes = v
                            .get("worktree_bytes")
                            .and_then(|x| x.as_u64())
                            .unwrap_or(0);
                        Ok(MachineHealthResult {
                            version,
                            worktree_bytes,
                        })
                    }
                    Err(e) => Err(format!("json: {}", e)),
                },
                Err(e) => Err(e.to_string()),
            },
            Err(e) => Err(e.to_string()),
        };
        let _ = tx.send(result);
    });
    rx
}

/// How many samples to keep per machine (5 min @ 5 s/sample).
pub(crate) const METRICS_HISTORY: usize = 60;
/// How often to poll each reachable machine's `/metrics` endpoint.
pub(crate) const METRICS_CADENCE: Duration = Duration::from_secs(5);

/// One `/metrics` snapshot from a remote agent.
#[derive(Clone, Copy)]
pub(crate) struct MetricSample {
    pub(crate) cpu: f32,
    pub(crate) mem: f32,
}

/// In-flight metrics fetch for one machine.
pub(crate) struct PendingMetrics {
    pub(crate) machine: String,
    pub(crate) rx: std::sync::mpsc::Receiver<Result<MetricSample, String>>,
}

/// Spawn a background thread that fetches `/metrics` from a remote agent.
pub(crate) fn spawn_machine_metrics(host: &str, port: u16, machine: String) -> PendingMetrics {
    let (tx, rx) = std::sync::mpsc::channel();
    let url = format!("http://{}:{}/metrics", host, port);
    std::thread::spawn(move || {
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(std::time::Duration::from_secs(2))
            .timeout(std::time::Duration::from_secs(3))
            .build();
        let result = match agent.get(&url).call() {
            Ok(resp) => match resp.into_string() {
                Ok(body) => match serde_json::from_str::<serde_json::Value>(&body) {
                    Ok(v) => {
                        let cpu = v
                            .get("cpu_percent")
                            .and_then(|x| x.as_f64())
                            .unwrap_or(0.0) as f32;
                        let mem = v
                            .get("mem_percent")
                            .and_then(|x| x.as_f64())
                            .unwrap_or(0.0) as f32;
                        Ok(MetricSample { cpu, mem })
                    }
                    Err(e) => Err(format!("json: {}", e)),
                },
                Err(e) => Err(e.to_string()),
            },
            Err(e) => Err(e.to_string()),
        };
        let _ = tx.send(result);
    });
    PendingMetrics { machine, rx }
}

/// One file entry from the agent's `/artifact/<repo>/<branch>` manifest.
/// Fields are parsed from JSON for completeness; the current UI uses only
/// the count and `ArtifactManifest::total_bytes` for the badge line.
#[derive(Clone)]
pub(crate) struct ArtifactFile {
    #[allow(dead_code)]
    pub(crate) name: String,
    #[allow(dead_code)]
    pub(crate) size: u64,
}

/// Parsed manifest returned by `GET /artifact/<repo>/<branch>` on an agent.
#[derive(Clone)]
pub(crate) struct ArtifactManifest {
    pub(crate) files: Vec<ArtifactFile>,
    pub(crate) total_bytes: u64,
    /// The assignment that produced this stash (may differ from the current
    /// work assignment when the branch was rebuilt on a later push).
    pub(crate) built_by_assignment_id: Option<String>,
}

/// Reason why no artifact manifest is available after a completed fetch.
/// Used to surface a human-readable explanation in the TUI rather than
/// silently hiding the `[a]` badge when artifacts are absent.
#[derive(Debug, Clone)]
pub(crate) enum ArtifactAbsence {
    /// HTTP 404 — worker did not stash any artifacts.  Likely causes: no
    /// `artifact_paths` configured for this repo in `coordinator.yml`, or the
    /// build produced no files matching the configured globs.
    NotStashed,
    /// HTTP 200 but the `files` array in the manifest was empty.
    ManifestEmpty,
    /// Could not reach the agent at all (connection refused, timeout, DNS
    /// failure, or a JSON-parse error on the response body).
    AgentUnreachable(String),
}

/// Whether a change for this issue/repo is *expected* to produce the
/// configured build artifact.  claude-coordinator's only `artifact_paths`
/// entry is `tui/target/debug/coord-tui`, produced solely by `tui/` changes;
/// a `coord/**` CLI/Python change never builds it (titled `coord:` vs the
/// `coord-tui:` convention for TUI work).  For such a change an empty stash
/// is *expected*, not a failure — the test path is a branch checkout, not an
/// artifact pull.  Other repos are build-centric, so every change produces
/// their artifact.
pub(crate) fn issue_produces_build_artifact(repo: &str, title: &str) -> bool {
    if repo == "claude-coordinator" {
        title.to_lowercase().contains("coord-tui")
    } else {
        true
    }
}

/// Actionable explanation for an empty/absent artifact stash, told apart by
/// whether the change was expected to build an artifact at all.  Stops an
/// empty stash from reading as a generic failure (#563/#569): a CLI change
/// has nothing to pull (test the branch directly); a build-producing change
/// with an empty stash means the session exited without a successful build.
pub(crate) fn artifact_absence_body(produces_artifact: bool, branch: &str) -> String {
    if produces_artifact {
        format!(
            "No build artifact stashed for branch `{branch}`.\n\
             A build was expected but the session exited without producing \
             the configured artifact (no successful build, or nothing matched \
             artifact_paths).\n\n\
             To test, check out the branch and build it locally:\n  \
             git fetch origin && git checkout {branch}\n  \
             # then the project's build (for coord-tui: `cd tui && cargo build \
             && cp target/debug/coord-tui ~/.local/bin/coord-tui`)"
        )
    } else {
        format!(
            "No artifact for branch `{branch}` — and none is expected.\n\
             This is a coord/ CLI/Python change; it doesn't build the coord-tui \
             binary, so there is nothing to pull.\n\n\
             Test it from the branch instead:\n  \
             git fetch origin && git checkout {branch}   # then run `coord ...`\n  \
             (or `pip install -e <worktree>` in a throwaway venv)"
        )
    }
}

/// A cached manifest entry with a fetch timestamp for 30-second TTL eviction.
pub(crate) struct ArtifactCacheEntry {
    pub(crate) fetched_at: Instant,
    /// `Some` = stash present and non-empty.  `None` = fetch completed but no
    /// artifacts are available; see `absence_reason` for the specific cause.
    pub(crate) manifest: Option<ArtifactManifest>,
    /// Explains why `manifest` is `None` when set.  Always `Some` when the
    /// fetch has completed without finding a non-empty manifest.
    pub(crate) absence_reason: Option<ArtifactAbsence>,
}

/// #1337: one hydrated (or failed) full-findings detail fetch — see
/// `CoordApp::findings_detail_cache`.
pub(crate) struct FindingsDetailEntry {
    pub(crate) fetched_at: Instant,
    /// `Some(raw)` = the full `review_findings` JSON string from
    /// `GET /assignment/{id}`.  `None` = the fetch failed; re-armed after a
    /// 30 s back-off so a down daemon isn't hammered every tick.
    pub(crate) full: Option<String>,
}

/// #336: Sanitize a git branch name for use as a URL path component.
///
/// Mirrors Python's `coord.agent._sanitize_branch`: replaces runs of
/// characters that are not alphanumeric, `.`, `_`, or `-` with a single dash,
/// then strips any leading/trailing dashes from the result.
pub(crate) fn sanitize_branch(branch: &str) -> String {
    let mut result = String::with_capacity(branch.len());
    let mut in_run = false;
    for c in branch.chars() {
        if c.is_alphanumeric() || c == '.' || c == '_' || c == '-' {
            in_run = false;
            result.push(c);
        } else if !in_run {
            result.push('-');
            in_run = true;
        }
    }
    result.trim_matches('-').to_string()
}

/// #349: Parse the JSON blob from `assignments.test_plan` into a Vec of
/// [`TestPlanStep`]s.  Returns `None` on any parse failure so callers
/// degrade gracefully to the "Preparing plan…" placeholder.
///
/// The expected JSON shape is:
/// ```json
/// {"steps": [{"kind": "run"|"pull"|"verify", "cmd": "…", "label": "…", "check": "…"}],
///  "blockers": ["…"]}
/// ```
pub(crate) fn parse_test_plan_steps(raw: &str) -> Option<Vec<TestPlanStep>> {
    let val: serde_json::Value = serde_json::from_str(raw).ok()?;
    let steps = val.get("steps")?.as_array()?;
    let result: Vec<TestPlanStep> = steps
        .iter()
        .filter_map(|s| {
            let kind = s.get("kind")?.as_str()?.to_string();
            Some(TestPlanStep {
                kind,
                cmd: s.get("cmd").and_then(|v| v.as_str()).map(|s| s.to_string()),
                label: s
                    .get("label")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string()),
                check: s
                    .get("check")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string()),
            })
        })
        .collect();
    Some(result)
}

/// #349: Read the current HEAD SHA for a git branch by examining the local
/// `.git` directory directly — fast (just file I/O) and safe to call from
/// the render thread.  Returns `None` when the file doesn't exist, is
/// unreadable, or the branch is not yet known locally.
///
/// Handles both loose refs (`refs/heads/<branch>`) and packed refs
/// (`packed-refs` file).
pub(crate) fn read_git_branch_head(repo_dir: &std::path::Path, branch: &str) -> Option<String> {
    use std::fs;
    // First try the loose ref file: .git/refs/heads/<branch>.
    // Branch names may contain slashes (feature/foo), which map to subdirs.
    let loose = repo_dir
        .join(".git")
        .join("refs")
        .join("heads")
        .join(branch);
    if let Ok(content) = fs::read_to_string(&loose) {
        let sha = content.trim().to_string();
        if !sha.is_empty() {
            return Some(sha);
        }
    }
    // Fall back to .git/packed-refs.  Format: "<sha> refs/heads/<branch>"
    let packed = repo_dir.join(".git").join("packed-refs");
    if let Ok(content) = fs::read_to_string(&packed) {
        let needle = format!("refs/heads/{}", branch);
        for line in content.lines() {
            if line.starts_with('#') || line.starts_with('^') {
                continue;
            }
            let mut parts = line.splitn(2, ' ');
            let sha = parts.next()?;
            let refname = parts.next()?;
            if refname.trim() == needle {
                return Some(sha.trim().to_string());
            }
        }
    }
    None
}

/// Outcome of a single `GET /artifact/<repo>/<branch>` request to a remote
/// agent.  Returned via channel from `spawn_artifact_fetch` so the TUI can
/// surface a specific reason when the `[a]` badge is absent rather than
/// silently hiding it.
pub(crate) enum ArtifactFetchOutcome {
    /// HTTP 200 with at least one file — the artifact badge should be shown.
    Found(ArtifactManifest),
    /// HTTP 404 — no stash exists for this (repo, branch) pair on the agent.
    NotStashed,
    /// HTTP 200 but the `files` array in the manifest was empty.
    Empty,
    /// Network / parse error — the agent could not be reached or returned
    /// an unexpected response.
    Unreachable(String),
}

/// #336: Spawn a background thread that queries `GET /artifact/<repo>/<branch>`
/// on a remote agent.  Returns a channel that delivers an [`ArtifactFetchOutcome`]
/// so the caller can distinguish 404, empty manifest, and network errors.
pub(crate) fn spawn_artifact_fetch(
    host: &str,
    repo: &str,
    branch: &str,
) -> std::sync::mpsc::Receiver<ArtifactFetchOutcome> {
    let url = format!("http://{}:7433/artifact/{}/{}", host, repo, branch);
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(std::time::Duration::from_secs(3))
            .timeout(std::time::Duration::from_secs(5))
            .build();
        let outcome = match agent.get(&url).call() {
            Err(ureq::Error::Status(404, _)) => ArtifactFetchOutcome::NotStashed,
            Err(e) => ArtifactFetchOutcome::Unreachable(e.to_string()),
            Ok(resp) => match resp.into_string() {
                Err(e) => ArtifactFetchOutcome::Unreachable(e.to_string()),
                Ok(body) => match serde_json::from_str::<serde_json::Value>(&body) {
                    Err(e) => ArtifactFetchOutcome::Unreachable(format!("json: {e}")),
                    Ok(v) => {
                        let files: Vec<ArtifactFile> = v
                            .get("files")
                            .and_then(|f| f.as_array())
                            .map(|arr| {
                                arr.iter()
                                    .filter_map(|item| {
                                        let name = item.get("name")?.as_str()?.to_string();
                                        let size =
                                            item.get("size").and_then(|s| s.as_u64()).unwrap_or(0);
                                        Some(ArtifactFile { name, size })
                                    })
                                    .collect()
                            })
                            .unwrap_or_default();
                        let total_bytes =
                            v.get("total_bytes").and_then(|t| t.as_u64()).unwrap_or(0);
                        let built_by_assignment_id = v
                            .get("built_by_assignment_id")
                            .and_then(|b| b.as_str())
                            .map(|s| s.to_string());
                        if files.is_empty() {
                            ArtifactFetchOutcome::Empty
                        } else {
                            ArtifactFetchOutcome::Found(ArtifactManifest {
                                files,
                                total_bytes,
                                built_by_assignment_id,
                            })
                        }
                    }
                },
            },
        };
        let _ = tx.send(outcome);
    });
    rx
}

/// Outcome of a single `GET /audit` request (#1039), delivered via channel
/// from `spawn_audit_fetch`.
pub(crate) enum AuditFetchOutcome {
    /// HTTP 200 with a parsed `AuditPage` (may have zero entries).
    Page(AuditPage),
    /// No board service is configured (`resolve_board_service` returned
    /// `None`) — the Audit panel has nothing to fetch from in this mode
    /// (local-SQLite-mode has no `/audit` HTTP surface). Rendered the same
    /// as "fetch not yet completed" (contract §4b empty state).
    NoBoardService,
    /// Network / parse error, or a non-2xx HTTP status.
    Unreachable(String),
}

/// How often to re-fetch `/audit` while the Audit panel is visible (#1039).
/// Mirrors the 30 s TTL `ArtifactCacheEntry` uses for the Pipeline Test
/// stage — audit entries are append-only and low-volume, so a slightly
/// slower cadence than the 5 s machine-metrics poll is plenty responsive.
pub(crate) const AUDIT_FETCH_TTL: Duration = Duration::from_secs(15);

/// #1039/#1040: spawn a background thread that fetches the first page of
/// `GET /audit` from the configured board service (`resolve_board_service`),
/// mirroring `spawn_artifact_fetch`'s thread-per-request pattern. Armed by
/// the caller only while `active_view == SidebarView::Audit` (see the poll
/// loop in `settings_ui.rs`), same gating discipline as the Machines-panel
/// metrics poll above.
///
/// `since`/`category`/`event_type` are the Audit panel's current filter
/// selection (contract §8/§9/§11, `tests/acceptance/ms-33/contract.md`) —
/// `None`/empty means "no filter", matching `/audit`'s own optional-param
/// semantics (#1037). Values ride as `ureq` query params (not manually
/// interpolated into the URL) so free-text `event_type` input never needs
/// its own percent-encoding.
pub(crate) fn spawn_audit_fetch(
    since: Option<f64>,
    category: Option<&str>,
    event_type: Option<&str>,
) -> std::sync::mpsc::Receiver<AuditFetchOutcome> {
    let (tx, rx) = std::sync::mpsc::channel();
    let Some((url, token)) = resolve_board_service() else {
        let _ = tx.send(AuditFetchOutcome::NoBoardService);
        return rx;
    };
    let category = category.map(|s| s.to_string());
    let event_type = event_type.map(|s| s.to_string());
    std::thread::spawn(move || {
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(std::time::Duration::from_secs(5))
            .timeout(std::time::Duration::from_secs(8))
            .build();
        let mut req = agent.get(&format!("{url}/audit"));
        if let Some(since) = since {
            req = req.query("since", &since.to_string());
        }
        if let Some(category) = &category {
            req = req.query("category", category);
        }
        if let Some(event_type) = &event_type {
            req = req.query("type", event_type);
        }
        if let Some(t) = &token {
            req = req.set("Authorization", &format!("Bearer {t}"));
        }
        let outcome = match req.call() {
            Ok(resp) => match resp.into_string() {
                Ok(body) => match serde_json::from_str::<AuditPage>(&body) {
                    Ok(page) => AuditFetchOutcome::Page(page),
                    Err(e) => AuditFetchOutcome::Unreachable(format!("json: {e}")),
                },
                Err(e) => AuditFetchOutcome::Unreachable(e.to_string()),
            },
            Err(e) => AuditFetchOutcome::Unreachable(e.to_string()),
        };
        let _ = tx.send(outcome);
    });
    rx
}

/// #315: signal that `spawn_inject_post` sends to the main thread when
/// the /inject POST returns HTTP 409 ("assignment is `done`") or 410
/// (BrokenPipeError — worker stdin closed).  Both mean the worker exited
/// after submit_inject's `worker_done` check but before the HTTP request
/// landed — a race window of a few hundred ms.  The main thread reacts
/// by dispatching `coord chat-continue` so the message isn't lost.
#[derive(Clone)]
pub(crate) struct InjectFallback {
    pub(crate) aid: String,
    pub(crate) text: String,
    pub(crate) issue_number: u64,
}

/// #264: POST a chat user-turn to a remote agent's `/inject/{id}` endpoint
/// in a background thread.  Used by `submit_inject` to bypass the
/// single-slot `command_runner` so chat submits aren't blocked by the
/// auto-`coord notify` cycle (every 30 s while any assignment is running).
///
/// #315: on HTTP 409/410 (worker exited mid-flight), sends an
/// `InjectFallback` over `fallback_tx` so the main thread can transparently
/// trigger `coord chat-continue` — otherwise the typed message would be
/// silently lost when the racing worker-exit beats the inject POST.
pub(crate) fn spawn_inject_post(
    host: &str,
    assignment_id: &str,
    text: &str,
    issue_number: u64,
    fallback_tx: std::sync::mpsc::Sender<InjectFallback>,
) {
    let url = format!("http://{}:7433/inject/{}", host, assignment_id);
    let payload = serde_json::json!({ "text": text });
    let body = payload.to_string();
    let aid = assignment_id.to_string();
    let text_owned = text.to_string();
    std::thread::spawn(move || {
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(std::time::Duration::from_secs(5))
            .timeout(std::time::Duration::from_secs(15))
            .build();
        match agent
            .post(&url)
            .set("Content-Type", "application/json")
            .send_string(&body)
        {
            Ok(_) => {}
            Err(ureq::Error::Status(code, _)) if code == 409 || code == 410 => {
                // Worker exited mid-flight — signal the main thread so it can
                // transparently fall back to `coord chat-continue`.  The user's
                // typed message is preserved on the channel.
                let _ = fallback_tx.send(InjectFallback {
                    aid,
                    text: text_owned,
                    issue_number,
                });
            }
            Err(e) => {
                eprintln!("[chat inject] POST {} failed: {}", url, e);
            }
        }
    });
}

/// #315: shell `coord [--config <path>] chat-continue <old_aid> <text>` in a
/// background thread.  Fire-and-forget — the TUI does not capture stdout here;
/// instead, `maybe_bind_pending_resume` polls `self.data.assignments` each tick
/// for the new row that `coord chat-continue` inserts into the coordinator DB.
///
/// Uses a raw thread rather than `CommandRunner` so the auto-`coord notify`
/// cycle (single-slot) is never blocked during an active chat session.
pub(crate) fn spawn_chat_continue(
    config_path: Option<std::path::PathBuf>,
    old_assignment_id: String,
    text: String,
) {
    std::thread::spawn(move || {
        let mut cmd = std::process::Command::new("coord");
        // Inject --config immediately after the subcommand name, mirroring the
        // CommandRunner pattern so `coord` finds coordinator.yml.
        cmd.arg("chat-continue");
        if let Some(ref cfg) = config_path {
            cmd.args(["--config", &cfg.to_string_lossy()]);
        }
        cmd.arg(&old_assignment_id);
        // #335: pass the whole message as a single argv entry. `Command` does
        // not go through a shell, so quotes/semicolons/dollar signs are already
        // literal. Splitting on whitespace was actively harmful: tokens
        // beginning with `-` (e.g. user types "claude -p" or "-v for verbose")
        // arrive at Click as unknown options and chat-continue aborts before
        // dispatching, silently dropping the user's turn.
        cmd.arg(&text);
        // #315: capture stderr (only) and surface non-zero exits.  Success
        // is silent; failure logs a single line so a future regression
        // can't disappear into /dev/null the way the original
        // fire-and-forget did.  Failures still surface to the user via
        // the bind-timeout toast even without this log.
        let aid_short: String = old_assignment_id.chars().take(6).collect();
        if let Ok(out) = cmd
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::piped())
            .output()
        {
            if !out.status.success() {
                let stderr = String::from_utf8_lossy(&out.stderr).trim().to_string();
                eprintln!(
                    "[chat-continue] FAILED for old_aid={} status={:?}: {}",
                    aid_short,
                    out.status.code(),
                    stderr,
                );
            }
        }
    });
}

/// Extract the `<!-- coord:review ... -->` header from a review body.
/// Returns `None` when the header is missing or malformed.  Tolerates
/// extra whitespace and unknown tokens — only `verdict` is required.
pub(crate) fn parse_coord_review_header(body: &str) -> Option<CoordReviewHeader> {
    let start = body.find("<!--").and_then(|s| {
        // Find a `coord:review` token within the same comment.
        let rest = &body[s..];
        let end = rest.find("-->")?;
        let inside = &rest[4..end];
        let trimmed = inside.trim();
        if !trimmed.starts_with("coord:review") && !trimmed.starts_with("coord: review") {
            return None;
        }
        let body_after = trimmed.split_once("coord:review").map(|(_, b)| b)?;
        Some(body_after.trim().to_string())
    })?;

    let mut header = CoordReviewHeader::default();
    for token in start.split_whitespace() {
        let (k, v) = match token.split_once('=') {
            Some(pair) => pair,
            None => continue,
        };
        let k_lower = k.to_ascii_lowercase();
        match k_lower.as_str() {
            "verdict" => header.verdict = Some(v.to_string()),
            "blocking" => header.blocking = v.parse().ok(),
            "nonblocking" => header.nonblocking = v.parse().ok(),
            "nits" => header.nits = v.parse().ok(),
            "reviewer" => header.reviewer = Some(v.to_string()),
            "assignment" => header.assignment = Some(v.to_string()),
            _ => {}
        }
    }
    if header.verdict.is_some() {
        Some(header)
    } else {
        None
    }
}

/// Parse `gh issue view --json comments` output into a `Vec<SessionSummary>`.
/// Returns entries newest-first.  Comments without coord markers are skipped.
///
/// `assignments` is passed so we can promote the `assignment_type` from the
/// local DB (the comment marker only carries the id, not the type).
///
/// #876: The live Summary tab now uses `build_board_summary_list_view` instead
/// (board-layer data, no GH shellout).  This function is kept for unit tests
/// that verify the comment-parsing logic in isolation.
#[cfg(test)]
pub(crate) fn parse_session_summaries_from_comments(
    comments_json: &serde_json::Value,
    assignments: &[Assignment],
) -> Vec<SessionSummary> {
    let arr = match comments_json.as_array() {
        Some(a) => a,
        None => return Vec::new(),
    };

    let mut entries: Vec<SessionSummary> = Vec::new();

    for comment in arr {
        let body = comment
            .get("body")
            .and_then(|b| b.as_str())
            .unwrap_or("");
        let created_at_str = comment
            .get("createdAt")
            .and_then(|c| c.as_str())
            .unwrap_or("");
        // Parse ISO-8601 "YYYY-MM-DDTHH:MM:SSZ" to a rough numeric timestamp
        // for sort ordering.  We only need relative ordering so a lexicographic
        // parse is fine (strings already sort correctly).
        let created_at_ts: f64 = {
            // Convert "2024-01-15T12:34:56Z" → keep it as-is for sort.
            // Store len as a proxy so newer > older (longer dates are not
            // necessarily later, but ISO-8601 strings sort lexicographically).
            // Better: try parse via a simple epoch conversion.
            parse_iso8601_to_epoch(created_at_str).unwrap_or(0.0)
        };

        // Try to parse a `<!-- coord:review ... -->` header first.
        if let Some(review_header) = parse_coord_review_header(body) {
            let assignment_id = review_header.assignment.clone().unwrap_or_default();
            let machine = review_header.reviewer.clone().unwrap_or_default();
            let verdict = review_header.verdict.clone();

            // Look up the local assignment to get the type.
            let session_type = assignments
                .iter()
                .find(|a| a.id == assignment_id)
                .and_then(|a| a.assignment_type.as_deref())
                .unwrap_or("review")
                .to_string();

            // Extract the prose summary: first non-empty line that isn't the
            // machine-readable header.
            let summary_text = extract_review_summary(body);

            entries.push(SessionSummary {
                assignment_id,
                session_type,
                machine,
                status: "done".to_string(),
                verdict,
                summary_text,
                created_at_ts,
            });
            continue;
        }

        // Try to parse a `<!-- coord:event=completion ... -->` or
        // `<!-- coord:event=failure ... -->` or `<!-- coord:event=advisory ... -->` header.
        if let Some(event_summary) = parse_coord_event_comment(body, assignments, created_at_ts) {
            entries.push(event_summary);
        }
    }

    // Newest-first.
    entries.sort_by(|a, b| {
        b.created_at_ts
            .partial_cmp(&a.created_at_ts)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    entries
}

/// Parse a `<!-- coord:event=... -->` comment into a `SessionSummary`.
/// Returns `None` when the comment doesn't carry a recognised coord event.
/// #876: test-only (called exclusively from `parse_session_summaries_from_comments`).
#[cfg(test)]
pub(crate) fn parse_coord_event_comment(
    body: &str,
    assignments: &[Assignment],
    created_at_ts: f64,
) -> Option<SessionSummary> {
    // Locate the first <!-- coord:... --> marker.
    let marker_start = body.find("<!--")?;
    let rest = &body[marker_start..];
    let end = rest.find("-->")?;
    let inside = rest[4..end].trim();
    if !inside.starts_with("coord:") {
        return None;
    }
    let after_coord = inside.strip_prefix("coord:")?.trim();

    // Parse key=value tokens.
    let mut event = "";
    let mut assignment_id = String::new();
    let mut machine = String::new();
    let mut _exit_code: Option<i32> = None;

    for token in after_coord.split_whitespace() {
        if let Some((k, v)) = token.split_once('=') {
            match k {
                "event" => event = v,
                "assignment" => assignment_id = v.to_string(),
                "machine" => machine = v.to_string(),
                "exit_code" => _exit_code = v.parse().ok(),
                _ => {}
            }
        }
    }

    let status = match event {
        "completion" => "done",
        "failure" => "failed",
        "advisory" => "advisory",
        // Skip briefings, stuck, plan, etc. — not terminal summaries.
        _ => return None,
    };

    // Look up assignment type from local DB.
    let session_type = assignments
        .iter()
        .find(|a| a.id == assignment_id)
        .and_then(|a| a.assignment_type.as_deref())
        .unwrap_or("work")
        .to_string();

    let summary_text = extract_completion_summary(body);

    Some(SessionSummary {
        assignment_id,
        session_type,
        machine,
        status: status.to_string(),
        verdict: None,
        summary_text,
        created_at_ts,
    })
}

/// Extract the prose from a `### Summary` block in a completion comment.
/// Returns the trimmed block text (may be multi-line), or empty string.
/// #876: test-only helper.
#[cfg(test)]
pub(crate) fn extract_completion_summary(body: &str) -> String {
    // Find "### Summary" heading and collect text until the next heading or end.
    let lower = body.to_ascii_lowercase();
    let Some(start) = lower.find("### summary") else {
        return String::new();
    };
    let after = &body[start + "### summary".len()..];
    let text: String = after
        .lines()
        .skip(1) // blank line after heading
        .take_while(|l| !l.starts_with('#'))
        .collect::<Vec<_>>()
        .join("\n");
    text.trim().to_string()
}

/// Extract a one-line prose summary from a review comment body.
/// Skips the `<!-- coord:review ... -->` header line and returns the first
/// non-empty content line.
/// #876: test-only helper.
#[cfg(test)]
pub(crate) fn extract_review_summary(body: &str) -> String {
    // Find "REVIEW_BODY:" marker if present (the structured review format).
    let lower = body.to_ascii_lowercase();
    if let Some(pos) = lower.find("review_body:") {
        let after = &body[pos + "review_body:".len()..];
        // Collect up to "END_REVIEW".
        let end = after
            .to_ascii_lowercase()
            .find("end_review")
            .unwrap_or(after.len());
        let block = &after[..end];
        // Return first non-empty line.
        for line in block.lines() {
            let t = line.trim();
            if !t.is_empty() {
                let truncated: String = t.chars().take(200).collect();
                return truncated;
            }
        }
    }
    // Fallback: first non-empty, non-header line.
    for line in body.lines() {
        let t = line.trim();
        if t.is_empty()
            || t.starts_with("<!--")
            || t.starts_with('#')
            || t.starts_with("**")
        {
            continue;
        }
        let truncated: String = t.chars().take(200).collect();
        return truncated;
    }
    String::new()
}

/// Very small ISO-8601 → Unix epoch converter.  Only handles the
/// `YYYY-MM-DDTHH:MM:SSZ` format that GitHub returns.  Returns `None` on
/// parse failure (the caller falls back to 0.0).
pub(crate) fn parse_iso8601_to_epoch(s: &str) -> Option<f64> {
    // Expected: "2024-01-15T12:34:56Z" (20 chars minimum)
    if s.len() < 19 {
        return None;
    }
    let year: i64 = s[0..4].parse().ok()?;
    let month: i64 = s[5..7].parse().ok()?;
    let day: i64 = s[8..10].parse().ok()?;
    let hour: i64 = s[11..13].parse().ok()?;
    let min: i64 = s[14..16].parse().ok()?;
    let sec: i64 = s[17..19].parse().ok()?;

    // Rough Julian-day-number → seconds calculation (ignores leap seconds).
    // Good enough for sorting; no external crate needed.
    let a: i64 = (14 - month) / 12;
    let y: i64 = year + 4800 - a;
    let m: i64 = month + 12 * a - 3;
    let jdn: i64 =
        day + (153 * m + 2) / 5 + 365 * y + y / 4 - y / 100 + y / 400 - 32045;
    // Unix epoch starts at JDN 2440588.
    let epoch_days = jdn - 2440588;
    let epoch_secs = epoch_days * 86400 + hour * 3600 + min * 60 + sec;
    Some(epoch_secs as f64)
}

pub(crate) fn home_dir() -> PathBuf {
    std::env::var("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("/root"))
}

pub(crate) fn coord_dir() -> PathBuf {
    home_dir().join(".coord")
}

/// TCP probe on port 7433 with a 150 ms deadline.
/// Hostname resolution is included in the deadline via a background thread.
pub(crate) fn tcp_probe(host: &str, port: u16) -> bool {
    use std::sync::mpsc;
    let host = host.to_string();
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let addr_str = format!("{}:{}", host, port);
        let ok = addr_str
            .to_socket_addrs()
            .ok()
            .and_then(|mut it| it.next())
            .map(|addr| TcpStream::connect_timeout(&addr, Duration::from_millis(120)).is_ok())
            .unwrap_or(false);
        let _ = tx.send(ok);
    });
    rx.recv_timeout(Duration::from_millis(200)).unwrap_or(false)
}

/// #778: compute staging entries from data already in memory.
///
/// Mirrors `coord.merge_queue.staging_items()` but runs in Rust using the
/// assignments and merge-queue entries already loaded from SQLite (or received
/// in the remote payload).  This keeps the local-DB path working without
/// requiring a `coord serve` daemon.
///
/// Gate checks performed:
/// 1. **Review gate** (when `"review"` is in `pipeline_default_gates`): the
///    work assignment must have a sibling review assignment with
///    `review_verdict = "approve"`.  Items without an approved review are
///    silently excluded (they're still "in pipeline", not "staging").
/// 2. **Smoke gate** (when `"test"` is in `pipeline_default_gates`): the
///    work assignment must carry `test_state = "passed"` or `"skipped"`.
///    Items that fail this gate appear as BLOCKED with reason
///    `"test verdict missing"`.
///
/// Items already in the merge queue (any state) and items from issues that
/// already have a MERGED queue entry are excluded.
pub(crate) fn compute_staging_local(
    assignments: &[Assignment],
    merge_queue: &[MergeQueueEntry],
    pipeline_default_gates: &[String],
) -> Vec<StagingEntry> {
    let review_gate = pipeline_default_gates.iter().any(|g| g == "review");
    let smoke_gate = pipeline_default_gates.iter().any(|g| g == "test");

    // Fast-lookup sets.
    let queued_aids: std::collections::HashSet<&str> =
        merge_queue.iter().map(|e| e.assignment_id.as_str()).collect();
    // Branch-level dedup (#778): a fix worker dispatched after the original
    // work was enqueued shares the same branch but has a different
    // assignment_id.  Exclude any assignment whose branch is already in the
    // queue so staging doesn't oscillate for the fix.
    let queued_branches: std::collections::HashSet<&str> = merge_queue
        .iter()
        .filter_map(|e| e.branch.as_deref())
        .collect();
    // Issue numbers for which a MERGED queue entry already exists.  We key
    // on issue_number only (no repo cross-check) because in the local path
    // MergeQueueEntry carries repo_github (the GitHub slug) while Assignment
    // carries repo (the coord-local name) — there is no reliable mapping
    // between the two without loading config.  False positives (two repos
    // with the same issue number) are extremely rare and the penalty is only
    // a temporarily missing staging row, so this approximation is acceptable.
    let merged_issue_numbers: std::collections::HashSet<u64> = merge_queue
        .iter()
        .filter(|e| e.state == "merged")
        .filter_map(|e| e.issue_number)
        .collect();

    // Build a quick look-up: assignment_id → list of (review_verdict) for
    // reviews that point to it.  We need this to check the review gate.
    // Key: work assignment_id; Value: true when at least one "approve" exists.
    let mut approved_aids: std::collections::HashSet<String> =
        std::collections::HashSet::new();
    for a in assignments {
        if a.assignment_type.as_deref() != Some("review") {
            continue;
        }
        if a.review_verdict.as_deref() != Some("approve") {
            continue;
        }
        if let Some(ref of_aid) = a.review_of_assignment_id {
            approved_aids.insert(of_aid.clone());
        }
    }

    let mut result: Vec<StagingEntry> = Vec::new();

    for a in assignments {
        if a.assignment_type.as_deref() != Some("work") {
            continue;
        }
        if a.status != "done" {
            continue;
        }
        let branch = match a.branch.as_deref() {
            Some(b) if !b.is_empty() => b.to_string(),
            _ => continue,
        };

        // Skip items already in the queue (by assignment_id or branch).
        // Branch-level dedup catches fix workers that share a branch with an
        // already-queued original work assignment (#778).
        if queued_aids.contains(a.id.as_str())
            || a.branch
                .as_deref()
                .map(|b| queued_branches.contains(b))
                .unwrap_or(false)
        {
            continue;
        }

        // Skip items from issues already MERGED.
        if merged_issue_numbers.contains(&a.issue_number) {
            continue;
        }

        // Review gate.
        if review_gate && !approved_aids.contains(&a.id) {
            continue; // not approved → not a staging item
        }

        // Smoke gate.
        let (status, reason) = if smoke_gate
            && !matches!(a.test_state.as_deref(), Some("passed") | Some("skipped"))
        {
            ("blocked".to_string(), Some("test verdict missing".to_string()))
        } else {
            ("ready".to_string(), None)
        };

        result.push(StagingEntry {
            assignment_id: a.id.clone(),
            repo_name: a.repo.clone(),
            issue_number: a.issue_number as i64,
            issue_title: a.issue_title.clone(),
            branch,
            status,
            reason,
        });
    }

    result
}

pub(crate) fn load_data() -> BoardData {
    // #584: when a board service is configured (env or ~/.coord/client.toml),
    // fetch the read-only board projection over HTTP from the `coord serve`
    // daemon instead of opening coord.db directly.  When NO service is
    // configured this falls through to the byte-identical SQLite path below.
    if let Some((url, token)) = resolve_board_service() {
        return load_data_remote(&url, token.as_deref());
    }

    let dir = coord_dir();
    let db_path = dir.join("coord.db");

    // Open the DB read-only; return empty data if the DB doesn't exist yet.
    let conn = match Connection::open_with_flags(
        &db_path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    ) {
        Ok(c) => c,
        Err(_) => return BoardData::default(),
    };

    // ── Query assignments ──────────────────────────────────────────────────
    // dispatched_at and finished_at are stored as REAL (Unix float seconds).
    let mut assignments: Vec<Assignment> = {
        let mut stmt = match conn.prepare(
            "SELECT assignment_id, machine_name, repo_name, issue_number, issue_title, \
             status, branch, model, type, dispatched_at, finished_at, exit_code, \
             test_state, review_verdict, review_of_assignment_id, cost_usd, \
             smoke_tests, review_findings, test_plan, test_plan_branch_head, \
             COALESCE(input_tokens, 0), COALESCE(output_tokens, 0), \
             COALESCE(cache_creation_tokens, 0), COALESCE(cache_read_tokens, 0), \
             COALESCE(is_interactive, 0), failure_reason, \
             COALESCE(review_iteration, 0), \
             acceptance_state, acceptance_reason, acceptance_sha, \
             acceptance_total, acceptance_passed, \
             test_reason, review_state, pr_url, \
             audit_goals_json, audit_bottom_line, audit_run_number, \
             for_issue_number \
             FROM assignments ORDER BY dispatched_at DESC",
        ) {
            Ok(s) => s,
            Err(_) => return BoardData::default(),
        };
        let rows = match stmt.query_map([], |row| {
            Ok(Assignment {
                id: row.get::<_, String>(0)?,
                machine: row.get::<_, String>(1)?,
                repo: row.get::<_, String>(2)?,
                issue_number: row.get::<_, i64>(3)? as u64,
                issue_title: row.get::<_, String>(4)?,
                status: row.get::<_, String>(5)?,
                branch: row.get::<_, Option<String>>(6)?,
                model: row.get::<_, Option<String>>(7)?,
                assignment_type: row.get::<_, Option<String>>(8)?,
                dispatched_at: row.get::<_, Option<f64>>(9)?,
                finished_at: row.get::<_, Option<f64>>(10)?,
                exit_code: row.get::<_, Option<i32>>(11)?,
                test_state: row.get::<_, Option<String>>(12)?,
                review_verdict: row.get::<_, Option<String>>(13)?,
                review_of_assignment_id: row.get::<_, Option<String>>(14)?,
                cost_usd: row.get::<_, Option<f64>>(15)?,
                smoke_tests: row
                    .get::<_, Option<String>>(16)?
                    .and_then(|raw| serde_json::from_str::<Vec<String>>(&raw).ok()),
                review_findings: row.get::<_, Option<String>>(17)?,
                // #349: parse test_plan JSON blob into Vec<TestPlanStep>.
                // Silently returns None on parse failure so older rows (no
                // column or malformed JSON) degrade to the "Preparing plan…"
                // placeholder rather than crashing.
                test_plan: row
                    .get::<_, Option<String>>(18)?
                    .and_then(|raw| parse_test_plan_steps(&raw)),
                test_plan_branch_head: row.get::<_, Option<String>>(19)?,
                // #546: token counts.  COALESCE(col, 0) in the SQL converts
                // any NULL values to 0 for existing columns.  If the columns
                // don't exist yet (pre-migration DB), the whole query fails
                // and BoardData::default() is returned — acceptable graceful
                // degradation since the Python coordinator always runs
                // migrations before workers produce any data to show.
                input_tokens: row.get::<_, i64>(20)?,
                output_tokens: row.get::<_, i64>(21)?,
                cache_creation_tokens: row.get::<_, i64>(22)?,
                cache_read_tokens: row.get::<_, i64>(23)?,
                // #546: is_interactive distinguishes Max-subscription sessions from
                // old automated rows that also have cost_usd=NULL + zero tokens.
                is_interactive: row.get::<_, i64>(24)? != 0,
                // #1337: the local-SQLite path reads the DB directly — the
                // wire-bounding policy applies only to the daemon's /board
                // payload, so findings here are always complete.
                review_findings_truncated: false,
                review_findings_len: None,
                // #618: short launch-failure reason; NULL for successful launches.
                // unwrap_or(None) absorbs a row.get() type-conversion error (e.g.
                // unexpected NULL type); a missing column causes conn.prepare() to
                // fail before any row is fetched, not here.
                failure_reason: row.get::<_, Option<String>>(25).unwrap_or(None),
                // #803: fix-round counter for model escalation on the interactive
                // --fix-of path.  COALESCE handles the pre-migration NULL case.
                review_iteration: row.get::<_, i64>(26)?,
                // #932/#944: the Acceptance-gate verdict, reported/gated
                // separately from test_state.
                acceptance_state: row.get::<_, Option<String>>(27)?,
                acceptance_reason: row.get::<_, Option<String>>(28)?,
                acceptance_sha: row.get::<_, Option<String>>(29)?,
                acceptance_total: row.get::<_, Option<i64>>(30)?,
                acceptance_passed: row.get::<_, Option<i64>>(31)?,
                // #876: board-sourced summary fields.  These columns may not
                // exist in pre-migration DBs, so we use unwrap_or(None) so an
                // older DB doesn't blank the board.
                test_reason: row.get::<_, Option<String>>(32).unwrap_or(None),
                review_state: row.get::<_, Option<String>>(33).unwrap_or(None),
                pr_url: row.get::<_, Option<String>>(34).unwrap_or(None),
                // #886 Phase 2: Milestone Outcome Audit columns. unwrap_or(None)
                // for the same graceful-degradation reason as pr_url above.
                audit_goals_json: row.get::<_, Option<String>>(35).unwrap_or(None),
                audit_bottom_line: row.get::<_, Option<String>>(36).unwrap_or(None),
                audit_run_number: row.get::<_, Option<i64>>(37).unwrap_or(None),
                // #1084: JIT test-author per-member-issue correlation.
                // unwrap_or(None) for the same graceful-degradation reason
                // as the audit columns above.
                for_issue_number: row
                    .get::<_, Option<i64>>(38)
                    .unwrap_or(None)
                    .map(|n| n as u64),
            })
        }) {
            Ok(r) => r,
            Err(_) => return BoardData::default(),
        };
        rows.filter_map(|r| r.ok()).collect()
    };

    // Sort: running first, then failed, then done (most recent first within groups).
    assignments.sort_by(|a, b| {
        let rank = |s: &str| match s {
            "running" => 0u8,
            "failed" => 1,
            "done" => 2,
            _ => 3,
        };
        rank(&a.status).cmp(&rank(&b.status)).then_with(|| {
            b.dispatched_at
                .partial_cmp(&a.dispatched_at)
                .unwrap_or(std::cmp::Ordering::Equal)
        })
    });

    // ── Query machines (name = nickname, host = Tailscale FQDN, repos = JSON array) ─
    let machine_rows: Vec<(String, String, Vec<String>)> = {
        let mut stmt = match conn.prepare("SELECT name, host, repos FROM machines") {
            Ok(s) => s,
            Err(_) => {
                return BoardData {
                    assignments,
                    ..BoardData::default()
                }
            }
        };
        let rows = match stmt.query_map([], |row| {
            let repos_json: String = row
                .get::<_, Option<String>>(2)?
                .unwrap_or_else(|| "[]".to_string());
            let repos: Vec<String> = serde_json::from_str(&repos_json).unwrap_or_default();
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?, repos))
        }) {
            Ok(r) => r,
            Err(_) => {
                return BoardData {
                    assignments,
                    ..BoardData::default()
                }
            }
        };
        rows.filter_map(|r| r.ok()).collect()
    };

    // ── Query merge_queue ──────────────────────────────────────────────────
    // Join to assignments to resolve issue_number (merge_queue may not have it).
    // A missing table (rare) degrades to an empty queue rather than dropping the
    // rest of the board — the assembly tail still runs (#584 shared with remote).
    let merge_queue: Vec<MergeQueueEntry> = {
        let stmt = conn.prepare(
            "SELECT mq.assignment_id, a.issue_number, mq.state, mq.pr_number, mq.pr_url, \
             mq.repo_github, mq.target_branch, mq.error, a.branch, mq.last_attempt \
             FROM merge_queue mq \
             LEFT JOIN assignments a ON mq.assignment_id = a.assignment_id",
        );
        match stmt {
            Ok(mut stmt) => stmt
                .query_map([], |row| {
                    Ok(MergeQueueEntry {
                        assignment_id: row.get::<_, String>(0)?,
                        issue_number: row.get::<_, Option<i64>>(1)?.map(|n| n as u64),
                        state: row.get::<_, String>(2)?,
                        pr_number: row.get::<_, Option<i64>>(3)?,
                        pr_url: row.get::<_, Option<String>>(4)?,
                        repo_github: row.get::<_, String>(5)?,
                        target_branch: row.get::<_, Option<String>>(6)?,
                        error: row.get::<_, Option<String>>(7)?,
                        // branch from assignments — used for branch-level dedup
                        // in compute_staging_local (#778).
                        branch: row.get::<_, Option<String>>(8)?,
                        // milestone_title is filled in by the client-side join
                        // in assemble_board_data after open_issues are loaded.
                        milestone_title: None,
                        // #913: merge time — see field doc on MergeQueueEntry.
                        last_attempt: row.get::<_, Option<f64>>(9)?,
                    })
                })
                .map(|rows| rows.filter_map(|r| r.ok()).collect())
                .unwrap_or_default(),
            Err(_) => Vec::new(),
        }
    };

    // ── Query proposals ───────────────────────────────────────────────────
    let proposals: Vec<Proposal> = {
        let stmt = conn.prepare(
            "SELECT id, machine_name, repo_name, issue_number, issue_title, \
             rationale, type FROM proposals ORDER BY id",
        );
        match stmt {
            Ok(mut stmt) => stmt
                .query_map([], |row| {
                    Ok(Proposal {
                        id: row.get::<_, i64>(0)?,
                        machine: row.get::<_, String>(1)?,
                        repo: row.get::<_, String>(2)?,
                        issue_number: row.get::<_, i64>(3)? as u64,
                        issue_title: row.get::<_, String>(4)?,
                        rationale: row.get::<_, Option<String>>(5)?.unwrap_or_default(),
                        proposal_type: row
                            .get::<_, Option<String>>(6)?
                            .unwrap_or_else(|| "work".into()),
                    })
                })
                .map(|rows| rows.filter_map(|r| r.ok()).collect())
                .unwrap_or_default(),
            Err(_) => Vec::new(),
        }
    };

    // ── Query synced issues (both open and closed) ─────────────────────────
    // Loaded eagerly so the Board Issue tab can show bodies for issues in
    // any lifecycle group, including closed ones in Completed. Only the
    // "open" entries are injected as Pending rows downstream.
    let open_issues: Vec<OpenIssue> = {
        let stmt = conn.prepare(
            "SELECT repo_name, number, title, body, labels, state, \
             milestone_number, milestone_title FROM issues \
             ORDER BY repo_name, number",
        );
        match stmt {
            Ok(mut stmt) => stmt
                .query_map([], |row| {
                    let labels_raw: String = row.get(4).unwrap_or_default();
                    let labels: Vec<String> =
                        serde_json::from_str(&labels_raw).unwrap_or_default();
                    Ok(OpenIssue {
                        repo_name: row.get::<_, String>(0)?,
                        number: row.get::<_, i64>(1)? as u64,
                        title: row.get::<_, String>(2)?,
                        body: row.get::<_, String>(3).unwrap_or_default(),
                        labels,
                        state: row
                            .get::<_, String>(5)
                            .unwrap_or_else(|_| "open".to_string()),
                        milestone_number: row.get::<_, Option<i64>>(6).unwrap_or(None),
                        milestone_title: row.get::<_, Option<String>>(7).unwrap_or(None),
                    })
                })
                .map(|rows| rows.filter_map(|r| r.ok()).collect())
                .unwrap_or_default(),
            Err(_) => Vec::new(),
        }
    };

    // ── Query board_meta for pipeline config ───────────────────────────────
    let (
        pipeline_default_gates,
        pipeline_tracked_labels,
        pipeline_repos,
        pipeline_require_plan,
        pipeline_repo_run_cmds,
        pipeline_repo_paths,
        pipeline_models,
        pipeline_acceptance_routes,
    ) = load_pipeline_meta(&conn);

    // ── Query cached structured plans ──────────────────────────────────────
    // Populated by `coord notify` parsing the plan worker's log into the
    // `plans` table.  The TUI renders these directly in the Plan stage
    // content panel — without this the panel falls back to dumping the
    // raw stream-json log (unreadable).
    let plans: std::collections::HashMap<String, PlanData> = {
        let mut out = std::collections::HashMap::new();
        if let Ok(mut stmt) = conn.prepare("SELECT assignment_id, plan_data FROM plans") {
            if let Ok(rows) = stmt.query_map([], |row| {
                let aid: String = row.get(0)?;
                let raw: String = row.get(1)?;
                Ok((aid, raw))
            }) {
                for r in rows.flatten() {
                    let (aid, raw) = r;
                    if let Ok(v) = serde_json::from_str::<serde_json::Value>(&raw) {
                        out.insert(aid, parse_plan_data(&v));
                    }
                }
            }
        }
        out
    };

    // #778: compute staging entries (approved/done work not yet in queue)
    // locally from already-loaded assignments + merge_queue so the staging
    // section works even without a coord serve daemon running.
    let merge_staging = compute_staging_local(
        &assignments,
        &merge_queue,
        &pipeline_default_gates,
    );

    assemble_board_data(
        assignments,
        machine_rows,
        open_issues,
        merge_queue,
        // Local SQLite path has no merge_plan; it is only available from the
        // remote /board endpoint (coord serve, #776).  Pass empty here.
        Vec::new(),
        proposals,
        plans,
        pipeline_default_gates,
        pipeline_tracked_labels,
        pipeline_repos,
        pipeline_repo_run_cmds,
        pipeline_repo_paths,
        pipeline_acceptance_routes,
        pipeline_require_plan,
        merge_staging,
        pipeline_models,
        // #550: local SQLite path has no daemon to compute the server-side
        // stage projection; `pipeline.rs`'s local functions it mirrors
        // remain authoritative on this path. Pass empty here.
        Vec::new(),
        // #795: local SQLite path has no daemon to compute the work-order
        // frontier. Pass empty; Pipeline cards show no rank/frontier badges.
        Vec::new(),
        // #1195/#1197: local SQLite path has no daemon to compute per-epic
        // children either. Pass empty; the Pipeline tree renders epics as
        // ordinary flat leaves on this path, same as before #1197.
        Vec::new(),
        // #975: local SQLite path has no daemon to compute the plan roster.
        // Pass empty; the Plans panel renders a "no plans yet" placeholder.
        Vec::new(),
        // #976: local SQLite path has no daemon at all, so plan_roster is
        // never "supported" here — the Plans panel must show its
        // not-connected-to-a-daemon message rather than treating the empty
        // Vec above as a genuinely empty roster.
        false,
        // #978: local SQLite path has no daemon to read GOAL.md from —
        // `GoalHeader::default()` (available: false) leaves the Plans panel
        // with no pinned header, exactly as before this field existed.
        GoalHeader::default(),
        // #1037/#1039: local SQLite path has no daemon computing the
        // 15-minute audit-recency window; the Audit panel's sidebar badge
        // simply shows nothing (0) on this path, same posture as
        // plan_roster_supported above.
        0,
    )
}

/// #584: run the machine reachability/health probes and assemble the final
/// [`BoardData`] from data already gathered by EITHER the local SQLite path
/// ([`load_data`]) or the remote `coord serve` /board path
/// ([`load_data_remote`]).
///
/// This is the shared tail of `load_data`: it spawns the per-machine TCP +
/// `/health` probes concurrently, derives `active_count` and the local-machine
/// name, and packs everything into `BoardData`.  Both callers feed it identical
/// inputs, so the probe + assembly behaviour is byte-identical regardless of
/// where the rows came from.
#[allow(clippy::too_many_arguments)]
pub(crate) fn assemble_board_data(
    assignments: Vec<Assignment>,
    machine_rows: Vec<(String, String, Vec<String>)>,
    open_issues: Vec<OpenIssue>,
    merge_queue: Vec<MergeQueueEntry>,
    merge_plan: Vec<PlannedMergeEntry>,
    proposals: Vec<Proposal>,
    plans: std::collections::HashMap<String, PlanData>,
    pipeline_default_gates: Vec<String>,
    pipeline_tracked_labels: Vec<String>,
    pipeline_repos: Vec<(String, String)>,
    pipeline_repo_run_cmds: std::collections::HashMap<String, String>,
    pipeline_repo_paths: std::collections::HashMap<String, String>,
    pipeline_acceptance_routes: std::collections::HashMap<String, Vec<String>>,
    pipeline_require_plan: bool,
    merge_staging: Vec<StagingEntry>,
    pipeline_models: Option<PipelineModels>,
    issue_stage_projection: Vec<IssueStageProjection>,
    milestone_work_orders: Vec<MilestoneWorkOrder>,
    epic_children: Vec<EpicChildren>,
    plan_roster: Vec<PlanRosterEntry>,
    plan_roster_supported: bool,
    goal_header: GoalHeader,
    audit_recent_count: u64,
) -> BoardData {
    // ── Machine reachability probes + health fetches ──────────────────────
    // Probe using the Tailscale host (fixes #121: machine name ≠ Tailscale hostname).
    // Spawn all TCP probes AND HTTP /health fetches concurrently so total
    // wall-clock time is bounded by the slowest machine, not N × timeout.
    let probes: Vec<(
        String,
        String,
        Vec<String>,
        std::sync::mpsc::Receiver<bool>,
        std::sync::mpsc::Receiver<Result<MachineHealthResult, String>>,
    )> = machine_rows
        .iter()
        .map(|(name, host, repos)| {
            use std::sync::mpsc;
            let h = host.clone();
            let (tcp_tx, tcp_rx) = mpsc::channel();
            std::thread::spawn(move || {
                let _ = tcp_tx.send(tcp_probe(&h, 7433));
            });
            let health_rx = spawn_machine_health(host, 7433);
            (name.clone(), host.clone(), repos.clone(), tcp_rx, health_rx)
        })
        .collect();

    let machines: Vec<Machine> = probes
        .into_iter()
        .map(|(name, host, repos, tcp_rx, health_rx)| {
            let tcp_reachable = tcp_rx
                .recv_timeout(Duration::from_millis(250))
                .unwrap_or(false);
            // Health fetch has a 2 s connect + read timeout baked in; we wait
            // up to 2.1 s here so we never block past the in-flight deadline.
            let health = health_rx
                .recv_timeout(Duration::from_millis(2100))
                .ok()
                .and_then(|r| r.ok());
            let reachable = tcp_reachable || health.is_some();
            let active_count = assignments
                .iter()
                .filter(|a| a.machine == name && a.status == "running")
                .count();
            Machine {
                name,
                host,
                reachable,
                active_count,
                repos,
                version: health.as_ref().map(|h| h.version.clone()),
                worktree_bytes: health.as_ref().map(|h| h.worktree_bytes).unwrap_or(0),
            }
        })
        .collect();

    // ── Determine which machine is local ──────────────────────────────────
    // Match the OS hostname against the `host` column in the machines table.
    // Hostnames are case-insensitive (DNS): the OS hostname is often mixed-case
    // (e.g. `john-HP-EliteBook-830-G7-Notebook-PC`) while coordinator.yml stores
    // it lower-case, so a case-sensitive compare never resolves the local
    // machine (#467 interactive launch broke on exactly this).
    let local_hostname = gethostname::gethostname().into_string().unwrap_or_default();
    let local_machine = machine_rows
        .iter()
        .find(|(_, host, _)| host.eq_ignore_ascii_case(&local_hostname))
        .map(|(name, _, _)| name.clone())
        .unwrap_or_default();

    // ── Client-side milestone join for merge_queue ────────────────────────
    // For each merge-queue entry, look up the milestone from open_issues on
    // (coord_repo_name, issue_number).  pipeline_repos maps coord repo name →
    // github slug; we reverse it to map entry.repo_github → coord repo name,
    // then scan open_issues for a matching row.
    let merge_queue: Vec<MergeQueueEntry> = merge_queue
        .into_iter()
        .map(|mut entry| {
            if let Some(issue_num) = entry.issue_number {
                let coord_repo = pipeline_repos
                    .iter()
                    .find(|(_, gh)| *gh == entry.repo_github)
                    .map(|(name, _)| name.as_str());
                if let Some(cr) = coord_repo {
                    if let Some(oi) = open_issues
                        .iter()
                        .find(|oi| oi.number == issue_num && oi.repo_name == cr)
                    {
                        entry.milestone_title = oi.milestone_title.clone();
                    }
                }
            }
            entry
        })
        .collect();

    BoardData {
        local_machine,
        assignments,
        open_issues,
        machines,
        merge_queue,
        merge_plan,
        proposals,
        pipeline_default_gates,
        pipeline_tracked_labels,
        pipeline_repos,
        pipeline_require_plan,
        pipeline_repo_run_cmds,
        pipeline_repo_paths,
        pipeline_acceptance_routes,
        plans,
        merge_staging,
        pipeline_models,
        issue_stage_projection,
        milestone_work_orders,
        epic_children,
        plan_roster,
        plan_roster_supported,
        goal_header,
        audit_recent_count,
    }
}

#[cfg(any(test, feature = "test-support"))]
thread_local! {
    /// #1087: test-only per-thread override for [`resolve_board_service`],
    /// set via [`set_test_board_service`]. Thread-local rather than a
    /// process env var deliberately: `resolve_board_service` runs
    /// synchronously on the *caller's* thread (before any network thread
    /// is spawned), so a thread-local override is visible exactly where a
    /// test needs it and invisible to every other test running
    /// concurrently on a different OS thread. `cargo test`'s default
    /// multi-threaded harness runs ~25 other Audit-panel tests
    /// (`tests.rs`, `SidebarView::Audit`) that also nudge
    /// `spawn_audit_fetch` via `run_periodic_work()` — a process-global
    /// `COORD_SERVICE_URL` mutation would let those transiently observe a
    /// mock URL they never opted into, an intermittent-failure trap this
    /// sidesteps entirely.
    static TEST_BOARD_SERVICE_OVERRIDE: std::cell::RefCell<Option<(String, Option<String>)>> =
        const { std::cell::RefCell::new(None) };
}

/// #1087: point `resolve_board_service()` at `url` (with optional bearer
/// `token`) for the remainder of the calling thread, so a test can exercise
/// `spawn_audit_fetch`'s real network path — e.g. against a
/// [`super::fixtures::MockBoardService`] — instead of the hard-coded
/// `AuditFetchOutcome::NoBoardService` every test build produced before
/// this seam existed. Returns an RAII guard that clears the override on
/// drop (including on test panic/assertion failure), so a `cargo test`
/// worker thread reused by a later, unrelated test never inherits stale
/// state.
///
/// `pub` (not `pub(crate)`) and re-exported from [`super::fixtures`]
/// (`coord_tui::fixtures::set_test_board_service` under the `test-support`
/// feature) so the same seam is available to an external integration-test
/// crate — e.g. a future sealed acceptance slice for #1087-shaped coverage
/// — exactly like `make_test_app` and friends already are.
#[cfg(any(test, feature = "test-support"))]
pub fn set_test_board_service(
    url: impl Into<String>,
    token: Option<String>,
) -> TestBoardServiceGuard {
    TEST_BOARD_SERVICE_OVERRIDE.with(|cell| *cell.borrow_mut() = Some((url.into(), token)));
    TestBoardServiceGuard(())
}

/// RAII guard returned by [`set_test_board_service`]; see its doc comment.
#[cfg(any(test, feature = "test-support"))]
pub struct TestBoardServiceGuard(());

#[cfg(any(test, feature = "test-support"))]
impl Drop for TestBoardServiceGuard {
    fn drop(&mut self) {
        TEST_BOARD_SERVICE_OVERRIDE.with(|cell| *cell.borrow_mut() = None);
    }
}

/// #584: resolve the configured board service URL + optional bearer token.
///
/// Precedence: environment (`COORD_SERVICE_URL` + `COORD_TOKEN`) wins over the
/// `~/.coord/client.toml` file (TOML keys `board_service` and optional `token`).
/// Returns `None` when no URL is found anywhere — the caller then falls back to
/// the local SQLite read path (byte-identical to today, no regression).
///
/// Any trailing `/` is stripped from the URL so callers can append `/board`.
pub(crate) fn resolve_board_service() -> Option<(String, Option<String>)> {
    // #1087: a test that opted in via `set_test_board_service` wins over the
    // hard test-mode short-circuit below — this is the seam that lets the
    // in-crate suite exercise `spawn_audit_fetch`'s real network path
    // end-to-end against a mock server. Checked first and scoped to this
    // thread only; see the doc comment on `set_test_board_service` for why
    // this isn't a `COORD_SERVICE_URL` env var instead.
    #[cfg(any(test, feature = "test-support"))]
    if let Some(value) = TEST_BOARD_SERVICE_OVERRIDE.with(|cell| cell.borrow().clone()) {
        return Some(value);
    }

    // In the test binary, treat the board service as absent.  This prevents
    // `record_test_verdict_remote`, `load_board_data_from_service`, and
    // `fetch_remote_config_to_cache` from firing real HTTP requests against
    // the production daemon during `cargo test`.  The `OnceLock` cache in
    // `is_remote_board_service()` would otherwise latch a developer-machine
    // value of `true` for the entire test process.
    //
    // #1039: also gated on `feature = "test-support"`, not just `cfg(test)`
    // — the sealed `tests/acceptance/**` suite (`tui/tests/acceptance.rs`,
    // the #1042 seam) is a separate integration-test binary, so the library
    // it links against is compiled *without* `cfg(test)` (only the
    // top-level test binary gets that cfg). Without this, a TuiDriver test
    // that navigates to the Audit panel would arm `spawn_audit_fetch` for
    // real against whatever `~/.coord/client.toml` happens to exist on the
    // machine running the suite — exactly the flaky/slow real-network trap
    // this function exists to avoid.  `cargo test --features test-support`
    // enables the feature crate-wide for that whole build (lib + every test
    // binary sharing the invocation), so this stays reliable there too.
    //
    // #1087: this is *not* bypassed by checking `COORD_SERVICE_URL` below —
    // a real board service is opted into via `set_test_board_service` above
    // instead, precisely to avoid a process-global env var a concurrently
    // running unrelated test could observe.
    #[cfg(any(test, feature = "test-support"))]
    return None;

    // Env first.
    #[allow(unreachable_code)]
    if let Ok(url) = std::env::var("COORD_SERVICE_URL") {
        let url = url.trim();
        if !url.is_empty() {
            let token = std::env::var("COORD_TOKEN")
                .ok()
                .map(|t| t.trim().to_string())
                .filter(|t| !t.is_empty());
            return Some((url.trim_end_matches('/').to_string(), token));
        }
    }

    // Then ~/.coord/client.toml.
    let path = coord_dir().join("client.toml");
    let text = std::fs::read_to_string(&path).ok()?;
    let parsed: toml::Value = toml::from_str(&text).ok()?;
    let url = parsed.get("board_service")?.as_str()?.trim();
    if url.is_empty() {
        return None;
    }
    let token = parsed
        .get("token")
        .and_then(|v| v.as_str())
        .map(|t| t.trim().to_string())
        .filter(|t| !t.is_empty());
    Some((url.trim_end_matches('/').to_string(), token))
}

/// #584: true when this coord-tui is a thin client of a remote `coord serve`
/// daemon (board fetched over HTTP).  Cached for the process lifetime — the
/// bootstrap (env / client.toml) doesn't change within a session.  Thin clients
/// must NOT auto-run host-side control commands (`coord notify`, `coord sync`):
/// they'd shell out locally against the wrong/absent DB and only produce error
/// toasts.  Routing these through the daemon is the write-path story (#590).
pub(crate) fn is_remote_board_service() -> bool {
    use std::sync::OnceLock;
    static CACHE: OnceLock<bool> = OnceLock::new();
    *CACHE.get_or_init(|| resolve_board_service().is_some())
}

/// #1012: shared JSON-POST-and-parse call to the board daemon, factored out
/// of `record_test_verdict_remote` (#590) so every subsequent direct-POST
/// mutation — starting with `apply_issue_labels_remote` — shares one
/// ureq agent/timeout/auth call site instead of hand-rolling it per
/// endpoint. Callers already resolved `url`/`token` via
/// [`resolve_board_service`] (they need the `Option` to decide whether to
/// fall back to a `coord` subprocess at all, so resolving here too would
/// just duplicate that branch).
///
/// `path` is appended verbatim to `url` (e.g. `"/issue-label"`); `body` is
/// serialized as the request JSON. Returns the parsed JSON response, or a
/// display-ready error string on any network/parse/non-2xx failure — `ureq`
/// (built without the `json` feature crate-wide here) already includes the
/// status code and a body snippet in its `Display` for non-2xx responses.
pub(crate) fn post_daemon_json(
    url: &str,
    token: Option<&str>,
    path: &str,
    body: &serde_json::Value,
) -> Result<serde_json::Value, String> {
    let body_str = serde_json::to_string(body).map_err(|e| format!("{e}"))?;
    let agent = ureq::AgentBuilder::new()
        .timeout_connect(std::time::Duration::from_secs(5))
        .timeout(std::time::Duration::from_secs(30))
        .build();
    let mut req = agent
        .post(&format!("{url}{path}"))
        .set("Content-Type", "application/json");
    if let Some(t) = token {
        req = req.set("Authorization", &format!("Bearer {t}"));
    }
    let resp = req.send_string(&body_str).map_err(|e| format!("{e}"))?;
    let text = resp.into_string().map_err(|e| format!("{e}"))?;
    serde_json::from_str(&text).map_err(|e| format!("{path}: invalid JSON response ({e}): {text}"))
}

/// #584: a thin client has no local `coordinator.yml`.  Fetch it from the daemon
/// (`GET /config`) once at startup and cache it to
/// `~/.coord/coordinator.remote.yml`, so the "coordinator.yml not found" status
/// warning clears and any `coord` subcommand has a config to point at (the
/// daemon owns the single source — #591).  Returns the cached path on success,
/// `None` on any network/IO error (the caller then leaves config_path as-is).
pub(crate) fn fetch_remote_config_to_cache() -> Option<std::path::PathBuf> {
    let (url, token) = resolve_board_service()?;
    let agent = ureq::AgentBuilder::new()
        .timeout_connect(std::time::Duration::from_secs(5))
        .timeout(std::time::Duration::from_secs(5))
        .build();
    let mut req = agent.get(&format!("{url}/config"));
    if let Some(t) = token.as_deref() {
        req = req.set("Authorization", &format!("Bearer {t}"));
    }
    let body = req.call().ok()?.into_string().ok()?;
    let dir = coord_dir();
    std::fs::create_dir_all(&dir).ok()?;
    let path = dir.join("coordinator.remote.yml");
    std::fs::write(&path, body).ok()?;
    Some(path)
}

/// #584: parse the pipeline_* keys out of a `board_meta` map fetched over the
/// /board wire.  Mirrors [`load_pipeline_meta`] (the SQLite reader) field for
/// field, including the documented fallbacks, so the remote path fills the
/// `BoardData.pipeline_*` fields identically to the local path.
pub(crate) fn parse_pipeline_meta_from_map(
    meta: &std::collections::HashMap<String, String>,
) -> (
    Vec<String>,
    Vec<String>,
    Vec<(String, String)>,
    bool,
    std::collections::HashMap<String, String>,
    std::collections::HashMap<String, String>,
    Option<PipelineModels>,
    std::collections::HashMap<String, Vec<String>>,
) {
    fn read_map(
        meta: &std::collections::HashMap<String, String>,
        key: &str,
    ) -> std::collections::HashMap<String, String> {
        meta.get(key)
            .and_then(|v| serde_json::from_str::<serde_json::Value>(v).ok())
            .and_then(|val| match val {
                serde_json::Value::Object(map) => Some(
                    map.into_iter()
                        .filter_map(|(k, v)| v.as_str().map(|s| (k, s.to_string())))
                        .collect(),
                ),
                _ => None,
            })
            .unwrap_or_default()
    }

    // #1151: repo_name -> list of route `match` globs (only present for
    // repos with a routed acceptance driver, #1125). Mirrors
    // `load_pipeline_meta`'s `read_map_of_lists` (the SQLite reader).
    fn read_map_of_lists(
        meta: &std::collections::HashMap<String, String>,
        key: &str,
    ) -> std::collections::HashMap<String, Vec<String>> {
        meta.get(key)
            .and_then(|v| serde_json::from_str::<serde_json::Value>(v).ok())
            .and_then(|val| match val {
                serde_json::Value::Object(map) => Some(
                    map.into_iter()
                        .filter_map(|(k, v)| {
                            let list = v.as_array()?;
                            let strs: Vec<String> = list
                                .iter()
                                .filter_map(|e| e.as_str().map(str::to_string))
                                .collect();
                            Some((k, strs))
                        })
                        .collect(),
                ),
                _ => None,
            })
            .unwrap_or_default()
    }

    let default_gates: Vec<String> = meta
        .get("pipeline_default_gates")
        .and_then(|v| serde_json::from_str::<Vec<String>>(v).ok())
        .unwrap_or_else(|| vec!["review".to_string(), "merge".to_string()]);

    let tracked_labels: Vec<String> = meta
        .get("pipeline_tracked_labels")
        .and_then(|v| serde_json::from_str::<Vec<String>>(v).ok())
        .unwrap_or_else(|| vec!["coord".to_string()]);

    let repos: Vec<(String, String)> = meta
        .get("pipeline_repos")
        .and_then(|v| serde_json::from_str::<serde_json::Value>(v).ok())
        .and_then(|val| match val {
            serde_json::Value::Object(map) => Some(
                map.into_iter()
                    .filter_map(|(k, v)| v.as_str().map(|s| (k, s.to_string())))
                    .collect(),
            ),
            _ => None,
        })
        .unwrap_or_default();

    let require_plan: bool = meta
        .get("pipeline_require_plan")
        .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
        .unwrap_or(false);

    let repo_run_cmds = read_map(meta, "pipeline_repo_run_cmds");
    let repo_paths = read_map(meta, "pipeline_repo_paths");

    // #803: model config snapshot — None when the daemon is pre-#803.
    let pipeline_models: Option<PipelineModels> = meta
        .get("pipeline_models")
        .and_then(|v| serde_json::from_str::<PipelineModels>(v).ok());

    // #1151: repo_name → route match globs — empty when the daemon predates
    // this field.
    let acceptance_routes = read_map_of_lists(meta, "pipeline_acceptance_routes");

    (
        default_gates,
        tracked_labels,
        repos,
        require_plan,
        repo_run_cmds,
        repo_paths,
        pipeline_models,
        acceptance_routes,
    )
}

/// #1336: last-known `/board` `(ETag, raw body)` for conditional GETs — see
/// `load_data_remote`.  Process-wide (one daemon per TUI process); guarded by
/// a Mutex because refresh ticks run on short-lived background threads.
static BOARD_ETAG_CACHE: std::sync::Mutex<Option<(String, String)>> =
    std::sync::Mutex::new(None);

/// #1337: fetch one assignment's FULL `review_findings` raw JSON string from
/// the daemon's single-assignment detail endpoint (`GET /assignment/{id}`).
/// The `/board` collection wire carries only a bounded preview
/// (`review_findings_truncated`); the Review stage pane hydrates the full
/// body through this.  Thread-per-request + channel, mirroring
/// [`spawn_artifact_fetch`].  Sends `None` on any HTTP/parse failure (the
/// pane keeps showing the preview).
pub(crate) fn spawn_findings_detail_fetch(
    base_url: String,
    token: Option<String>,
    assignment_id: String,
) -> std::sync::mpsc::Receiver<Option<String>> {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(std::time::Duration::from_secs(3))
            .timeout(std::time::Duration::from_secs(5))
            .build();
        let mut req = agent.get(&format!("{base_url}/assignment/{assignment_id}"));
        if let Some(t) = token {
            req = req.set("Authorization", &format!("Bearer {t}"));
        }
        let out = match req.call() {
            Ok(resp) => resp
                .into_string()
                .ok()
                .and_then(|body| serde_json::from_str::<serde_json::Value>(&body).ok())
                .and_then(|v| {
                    v.get("review_findings")
                        .and_then(|f| f.as_str())
                        .map(|s| s.to_string())
                }),
            Err(_) => None,
        };
        let _ = tx.send(out);
    });
    rx
}

/// #584: fetch the read-only board projection from the `coord serve` daemon
/// over HTTP and assemble it into a [`BoardData`] via the shared
/// [`assemble_board_data`] tail (so the machine probes still run exactly as the
/// local path does).
///
/// On ANY error — network failure, non-2xx status, or JSON parse mismatch —
/// returns `BoardData::default()` rather than panicking; the TUI's 5 s refresh
/// loop simply retries.
pub(crate) fn load_data_remote(url: &str, token: Option<&str>) -> BoardData {
    let agent = ureq::AgentBuilder::new()
        .timeout_connect(std::time::Duration::from_secs(8))
        .timeout(std::time::Duration::from_secs(8))
        .build();
    let mut req = agent.get(&format!("{url}/board"));
    if let Some(t) = token {
        req = req.set("Authorization", &format!("Bearer {t}"));
    }
    // #1336 invariant 5: cache-validated polling.  Send the last ETag as
    // If-None-Match; a 304 means the board hasn't changed, so re-parse the
    // cached body instead of re-downloading megabytes over Tailscale every
    // poll.  (This runs on the background refresh thread — the reparse never
    // blocks the UI.)
    let cached: Option<(String, String)> = BOARD_ETAG_CACHE
        .lock()
        .ok()
        .and_then(|guard| guard.clone());
    if let Some((etag, _)) = &cached {
        req = req.set("If-None-Match", etag);
    }
    // ureq's `json` feature isn't enabled, so read the body as a string and
    // parse with serde_json (already a dependency).
    let payload: BoardPayload = match req.call() {
        Ok(resp) if resp.status() == 304 => {
            // Not modified — the daemon validated our cached copy.
            let Some((_, body)) = cached else {
                return BoardData::default();
            };
            match serde_json::from_str::<BoardPayload>(&body) {
                Ok(p) => p,
                Err(_) => return BoardData::default(),
            }
        }
        Ok(resp) => {
            let etag = resp.header("etag").map(|e| e.to_string());
            match resp.into_string() {
                Ok(body) => match serde_json::from_str::<BoardPayload>(&body) {
                    Ok(p) => {
                        if let Some(etag) = etag {
                            if let Ok(mut guard) = BOARD_ETAG_CACHE.lock() {
                                *guard = Some((etag, body));
                            }
                        }
                        p
                    }
                    Err(_) => return BoardData::default(),
                },
                Err(_) => return BoardData::default(),
            }
        }
        Err(_) => return BoardData::default(),
    };

    let mut assignments = payload.assignments;
    // Sort: running first, then failed, then done (most recent first within
    // groups) — identical to the SQLite path.
    assignments.sort_by(|a, b| {
        let rank = |s: &str| match s {
            "running" => 0u8,
            "failed" => 1,
            "done" => 2,
            _ => 3,
        };
        rank(&a.status).cmp(&rank(&b.status)).then_with(|| {
            b.dispatched_at
                .partial_cmp(&a.dispatched_at)
                .unwrap_or(std::cmp::Ordering::Equal)
        })
    });

    let machine_rows: Vec<(String, String, Vec<String>)> = payload
        .machines
        .into_iter()
        .map(|m| (m.name, m.host, m.repos))
        .collect();

    let plans: std::collections::HashMap<String, PlanData> = payload
        .plans
        .iter()
        .map(|(aid, v)| (aid.clone(), parse_plan_data(v)))
        .collect();

    let (
        pipeline_default_gates,
        pipeline_tracked_labels,
        pipeline_repos,
        pipeline_require_plan,
        pipeline_repo_run_cmds,
        pipeline_repo_paths,
        pipeline_models,
        pipeline_acceptance_routes,
    ) = parse_pipeline_meta_from_map(&payload.board_meta);

    // #778: prefer the server-computed staging list from the /board payload;
    // fall back to local computation so the panel still works if the daemon
    // is running an older version that doesn't emit merge_staging yet.
    let merge_staging = if payload.merge_staging.is_empty() {
        compute_staging_local(
            &assignments,
            &payload.merge_queue,
            &pipeline_default_gates,
        )
    } else {
        payload.merge_staging
    };

    assemble_board_data(
        assignments,
        machine_rows,
        payload.issues,
        payload.merge_queue,
        payload.merge_plan,
        payload.proposals,
        plans,
        pipeline_default_gates,
        pipeline_tracked_labels,
        pipeline_repos,
        pipeline_repo_run_cmds,
        pipeline_repo_paths,
        pipeline_acceptance_routes,
        pipeline_require_plan,
        merge_staging,
        pipeline_models,
        // #550: prefer the server-computed stage projection; empty when the
        // daemon predates #550 (`pipeline.rs`'s local functions fall back).
        payload.issue_stage_projection,
        // #795: server-computed work-order rank + frontier; empty when the
        // daemon predates #795.
        payload.milestone_work_orders,
        // #1195/#1197: server-computed per-epic children; empty when the
        // daemon predates #1195 (the Pipeline tree renders epics as
        // ordinary flat leaves in that case).
        payload.epic_children,
        // #975: server-computed plan roster; empty when the daemon predates
        // #975 (the Plans panel shows a placeholder in that case).
        payload.plan_roster,
        // #976: capability flag distinguishing "empty roster" from "daemon
        // predates #975/#976 and never computed one" — see
        // `BoardData::plan_roster_supported`.
        payload.plan_roster_supported,
        // #978: server-computed GOAL.md north-star header; `available: false`
        // (the `Default`) on daemons that predate #978.
        payload.goal_header,
        // #1037/#1039: 15-minute audit-recency count for the Audit panel's
        // sidebar badge; `0` (`#[serde(default)]`) on daemons that predate
        // #1037.
        payload.audit_recent_count,
    )
}

/// Decode a JSON plan_data blob into a `PlanData`.  Mirrors
/// `coord.plan_parser.WorkerPlan.from_dict`; tolerant of missing fields.
pub(crate) fn parse_plan_data(v: &serde_json::Value) -> PlanData {
    fn s(v: &serde_json::Value, key: &str) -> String {
        v.get(key)
            .and_then(|x| x.as_str())
            .unwrap_or("")
            .to_string()
    }
    fn vs(v: &serde_json::Value, key: &str) -> Vec<String> {
        v.get(key)
            .and_then(|x| x.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|e| e.as_str().map(|s| s.to_string()))
                    .collect()
            })
            .unwrap_or_default()
    }
    // smoke_tests is tri-state: missing/null → None, [] → Some(empty),
    // non-empty list → Some(bullets).
    let smoke_tests = match v.get("smoke_tests") {
        Some(serde_json::Value::Array(arr)) => Some(
            arr.iter()
                .filter_map(|e| e.as_str().map(|s| s.to_string()))
                .collect(),
        ),
        _ => None,
    };
    PlanData {
        plan: s(v, "plan"),
        files_modify: vs(v, "files_modify"),
        approach: s(v, "approach"),
        risks: s(v, "risks"),
        estimate: s(v, "estimate"),
        smoke_tests,
    }
}

/// Spawn a background thread that calls [`load_data`] and sends the result
/// over a channel.  The caller polls the returned [`Receiver`] without
/// blocking the UI thread.
pub(crate) fn start_data_load() -> std::sync::mpsc::Receiver<BoardData> {
    let (tx, rx) = std::sync::mpsc::channel();
    // In the test binary, immediately resolve with an empty payload so that
    // apply_pending_data()'s degraded-tick guard fires and preserves the
    // BoardData seeded by make_test_app().  Without this guard, refreshes
    // triggered by view-switches (maybe_kick_pipeline_loader → refresh) read
    // the real local SQLite DB and overwrite pipeline_issues / data.assignments
    // with whatever the developer's coord.db currently contains, making
    // TuiDriver tests non-deterministic and machine-dependent.
    #[cfg(test)]
    {
        let _ = tx.send(BoardData::default());
        return rx;
    }
    #[allow(unreachable_code)]
    std::thread::spawn(move || {
        let _ = tx.send(load_data());
    });
    rx
}

/// One running interactive session discovered from `coord sessions --json`.
///
/// Sessions are named `coord-<assignment_id>` and survive TUI crashes;
/// the operator can reattach via `coord reattach <assignment_id>` or by
/// opening the Pipeline Terminal tab for the matching issue.
#[derive(Clone, Debug)]
pub(crate) struct LiveTmuxSession {
    /// The coordinator assignment ID extracted from the session name.
    pub(crate) assignment_id: String,
    /// GitHub issue number, if the assignment record is in the local DB.
    pub(crate) issue_number: Option<u64>,
    /// Coordinator-local repo name, if known.
    pub(crate) repo_name: Option<String>,
    /// Issue title, if known (for display purposes).  Shown in the startup
    /// toast so the operator recognises which work was in progress.
    #[allow(dead_code)]
    pub(crate) issue_title: Option<String>,
    /// Machine the session is hosted on, from `coord sessions --json`
    /// (`machine` field) or derived from the assignment record.  `None`
    /// for sessions that pre-date the field or whose machine is unknown.
    pub(crate) machine: Option<String>,
    /// `true` when the session's pane process (claude) has exited but the
    /// tmux session is still up — the detach-and-abandon / dead-pane case
    /// (#491).  `false` while the pane is still running or status is unknown
    /// (sessions that pre-date the `pane_dead` field default to `false`).
    pub(crate) pane_dead: bool,
    /// #935 (Part A): number of discovery sweeps this optimistic `"pending-"`
    /// entry has survived without being covered by a real session.  Only used
    /// on entries whose `assignment_id` starts with `"pending-"`; always 0 on
    /// real discovery entries.  When the count exceeds the budget (2 sweeps),
    /// `poll_remote_sessions` drops the entry so a phantom "Live" badge cannot
    /// linger forever when a session never actually started.
    pub(crate) pending_sweep_count: u8,
    /// #1031: `true` when a client is currently attached to the tmux
    /// session, from `coord sessions --json`'s `attached` key. Mirrors
    /// `FleetTerminal::attached`'s `#{session_attached}` handling. `false`
    /// for sessions discovered by a probe that pre-dates this field.
    /// Surfaced as a `[attached]` tag in the #1032 Sessions-panel tree.
    pub(crate) attached: bool,
}

/// Fetch live `coord-*` tmux sessions by running `coord sessions --json`.
///
/// Returns an empty `Vec` when tmux is not running, `coord` is not on PATH,
/// or parsing fails.  This is called once at startup — it's cheap but
/// synchronous so it runs before the TUI is visible.
pub(crate) fn fetch_live_tmux_sessions() -> Vec<LiveTmuxSession> {
    let out = std::process::Command::new("coord")
        .args(["sessions", "--json"])
        .output()
        .ok();
    let out = match out {
        Some(o) if o.status.success() => o,
        _ => return Vec::new(),
    };
    parse_sessions_json(&String::from_utf8_lossy(&out.stdout))
}

/// Parse the `{"sessions": [...]}` JSON emitted by `coord sessions --json`.
/// Shared by the synchronous local fetch and the background remote fetch.
pub(crate) fn parse_sessions_json(text: &str) -> Vec<LiveTmuxSession> {
    let v: serde_json::Value = match serde_json::from_str(text) {
        Ok(v) => v,
        Err(_) => return Vec::new(),
    };
    let arr = match v.get("sessions").and_then(|s| s.as_array()) {
        Some(a) => a,
        None => return Vec::new(),
    };
    arr.iter()
        .filter_map(|entry| {
            let assignment_id = entry.get("assignment_id")?.as_str()?.to_string();
            let issue_number = entry
                .get("issue_number")
                .and_then(|n| n.as_u64());
            let repo_name = entry
                .get("repo_name")
                .and_then(|n| n.as_str())
                .map(|s| s.to_string());
            let issue_title = entry
                .get("issue_title")
                .and_then(|n| n.as_str())
                .map(|s| s.to_string());
            let machine = entry
                .get("machine")
                .and_then(|n| n.as_str())
                .map(|s| s.to_string());
            // #491: "1" = pane process has exited; "0" or absent = alive.
            let pane_dead = entry
                .get("pane_dead")
                .and_then(|v| v.as_str())
                .map(|s| s == "1")
                .unwrap_or(false);
            // #1031: `attached` is a JSON bool (not a "0"/"1" string like
            // `pane_dead`) — see `coord/commands/sessions.py`'s
            // `sessions_cmd`. Absent for a probe that pre-dates the field.
            let attached = entry
                .get("attached")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            Some(LiveTmuxSession {
                assignment_id,
                issue_number,
                repo_name,
                issue_title,
                machine,
                pane_dead,
                pending_sweep_count: 0,
                attached,
            })
        })
        .collect()
}

/// #486 Leg 4: fetch local + REMOTE coord-* sessions in the background.
///
/// Runs `coord sessions --json --remote` (which ssh-probes the fleet) off the
/// startup path so the TUI appears immediately; the result REPLACES the
/// local-only startup snapshot when it arrives (it is a superset).  A missing
/// config path lets `coord` fall back to its own discovery.
pub(crate) fn spawn_remote_tmux_sessions_fetch(
    config_path: Option<std::path::PathBuf>,
) -> std::sync::mpsc::Receiver<Vec<LiveTmuxSession>> {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut args: Vec<String> =
            vec!["sessions".into(), "--json".into(), "--remote".into()];
        if let Some(cfg) = config_path {
            args.push("--config".into());
            args.push(cfg.to_string_lossy().into_owned());
        }
        let out = std::process::Command::new("coord").args(&args).output().ok();
        let sessions = match out {
            Some(o) if o.status.success() => {
                parse_sessions_json(&String::from_utf8_lossy(&o.stdout))
            }
            _ => Vec::new(),
        };
        let _ = tx.send(sessions);
    });
    rx
}

/// #953: one persistent, free-floating `coord-term-*` terminal discovered via
/// `coord terminal list --json` (#952). Distinct from [`LiveTmuxSession`]
/// (`coord-<assignment_id>` interactive claude sessions): a `FleetTerminal`
/// carries no issue/repo/assignment — it's a plain shell, grouped in the
/// Terminal view's left-pane tree by the machine it runs on.
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct FleetTerminal {
    /// The `coord-term-<slug>` slug (without the `coord-term-` prefix).
    pub(crate) name: String,
    /// Coordinator-local machine name (`coordinator.yml` `machines[].name`),
    /// matching `Machine.name` so the tree can group by parent.
    pub(crate) machine: String,
    /// `true` when a client currently has the tmux session attached.
    pub(crate) attached: bool,
    /// #954: `true` for an optimistic entry inserted by
    /// `create_and_attach_terminal` immediately on creation, before the next
    /// `coord terminal list` discovery sweep can confirm it. Always `false`
    /// on entries parsed from a real discovery result. Mirrors
    /// `LiveTmuxSession`'s `"pending-"`-prefix convention, but as an
    /// explicit field since `FleetTerminal` slugs carry no such prefix.
    pub(crate) pending: bool,
    /// #954: number of discovery sweeps a `pending` entry has survived
    /// without being covered by a real result. Only meaningful when
    /// `pending` is `true`; always 0 on real entries. Mirrors
    /// `LiveTmuxSession::pending_sweep_count` / `PENDING_SESSION_SWEEP_BUDGET`
    /// — `poll_remote_terminals` evicts the entry once this exceeds
    /// `PENDING_TERMINAL_SWEEP_BUDGET`, so a phantom entry that never
    /// becomes a real tmux session doesn't linger in the tree forever.
    pub(crate) pending_sweep_count: u8,
}

/// Fetch local `coord-term-*` terminals by running `coord terminal list --json`.
///
/// Mirrors [`fetch_live_tmux_sessions`]: returns an empty `Vec` when tmux is
/// not running, `coord` is not on PATH, or parsing fails. Called once at
/// startup — cheap but synchronous so it runs before the TUI is visible.
pub(crate) fn fetch_fleet_terminals() -> Vec<FleetTerminal> {
    let out = std::process::Command::new("coord")
        .args(["terminal", "list", "--json"])
        .output()
        .ok();
    let out = match out {
        Some(o) if o.status.success() => o,
        _ => return Vec::new(),
    };
    parse_fleet_terminals_json(&String::from_utf8_lossy(&out.stdout))
}

/// Parse the JSON emitted by `coord terminal list --json`.
///
/// Unlike `coord sessions --json`'s `{"sessions": [...]}` envelope, this is
/// a **bare JSON array** of `{"name","attached","machine","host",...}`
/// objects (see `coord/commands/terminal.py::terminal_list`). Shared by the
/// synchronous local fetch and the background remote fetch.
pub(crate) fn parse_fleet_terminals_json(text: &str) -> Vec<FleetTerminal> {
    let v: serde_json::Value = match serde_json::from_str(text) {
        Ok(v) => v,
        Err(_) => return Vec::new(),
    };
    let arr = match v.as_array() {
        Some(a) => a,
        None => return Vec::new(),
    };
    arr.iter()
        .filter_map(|entry| {
            let name = entry.get("name")?.as_str()?.to_string();
            let machine = entry
                .get("machine")
                .and_then(|m| m.as_str())
                .unwrap_or("")
                .to_string();
            let attached = entry
                .get("attached")
                .and_then(|a| a.as_bool())
                .unwrap_or(false);
            Some(FleetTerminal {
                name,
                machine,
                attached,
                pending: false,
                pending_sweep_count: 0,
            })
        })
        .collect()
}

/// #953: fetch local + REMOTE `coord-term-*` terminals in the background.
///
/// Mirrors [`spawn_remote_tmux_sessions_fetch`]: runs `coord terminal list
/// --json --remote` off the startup path so the TUI appears immediately;
/// the result REPLACES the local-only startup snapshot when it arrives.
pub(crate) fn spawn_remote_fleet_terminals_fetch(
    config_path: Option<std::path::PathBuf>,
) -> std::sync::mpsc::Receiver<Vec<FleetTerminal>> {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut args: Vec<String> = vec![
            "terminal".into(),
            "list".into(),
            "--json".into(),
            "--remote".into(),
        ];
        if let Some(cfg) = config_path {
            args.push("--config".into());
            args.push(cfg.to_string_lossy().into_owned());
        }
        let out = std::process::Command::new("coord").args(&args).output().ok();
        let terminals = match out {
            Some(o) if o.status.success() => {
                parse_fleet_terminals_json(&String::from_utf8_lossy(&o.stdout))
            }
            _ => Vec::new(),
        };
        let _ = tx.send(terminals);
    });
    rx
}

/// #603: fetch the EXACT fix briefing for `aid` (`coord fix-briefing <aid>`) off
/// the UI thread, so the fail→fix / rework confirm dialog can show the operator
/// what the fix worker will be briefed with.  stdout IS the briefing text; on
/// any failure a short human note is returned (the dialog still launches fine).
pub(crate) fn spawn_fix_briefing_fetch(
    aid: String,
    config_path: Option<std::path::PathBuf>,
) -> std::sync::mpsc::Receiver<String> {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        // `--config` is a per-subcommand option → it must come AFTER `aid`.
        let mut args: Vec<String> = vec!["fix-briefing".into(), aid];
        if let Some(cfg) = config_path {
            args.push("--config".into());
            args.push(cfg.to_string_lossy().into_owned());
        }
        let out = std::process::Command::new("coord").args(&args).output().ok();
        let text = match out {
            Some(o) if o.status.success() => String::from_utf8_lossy(&o.stdout).into_owned(),
            Some(o) => format!(
                "(could not resolve the fix briefing: {})",
                String::from_utf8_lossy(&o.stderr).trim()
            ),
            None => "(could not run `coord fix-briefing`)".to_string(),
        };
        let _ = tx.send(text);
    });
    rx
}

/// Return the version string of the local `coord` binary by running
/// `coord --version` synchronously.  Parses the last whitespace-separated
/// token from the first output line (e.g. "coord 0.4.1" → "0.4.1").
/// Returns `None` when `coord` is not found, exits non-zero, or returns
/// unparseable output.
pub(crate) fn fetch_local_coord_version() -> Option<String> {
    let out = std::process::Command::new("coord")
        .arg("--version")
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&out.stdout);
    text.lines()
        .next()
        .and_then(|l| l.split_whitespace().last())
        .map(|s| s.to_string())
}

/// Spawn a background thread that fetches a remote agent log over HTTP.
///
/// Returns a `Receiver` that yields `Ok(raw_content)` or `Err(error_message)`.
/// The caller must parse the content with [`parse_log_content`] on the main
/// thread — keeping `ListItem` construction off the worker thread.
pub(crate) fn spawn_log_fetch(host: &str, id: &str) -> std::sync::mpsc::Receiver<Result<String, String>> {
    let (tx, rx) = std::sync::mpsc::channel();
    let url = format!("http://{}:7433/logs/{}", host, id);
    std::thread::spawn(move || {
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(std::time::Duration::from_secs(5))
            .timeout(std::time::Duration::from_secs(5))
            .build();
        let result = match agent.get(&url).call() {
            Ok(resp) => resp.into_string().map_err(|e| e.to_string()),
            Err(e) => Err(e.to_string()),
        };
        let _ = tx.send(result);
    });
    rx
}

/// Spawn a `gh issue view` for a single issue and parse the response into a
/// [`FetchedIssue`]. Used by the Board Issue tab when the issue isn't in the
/// local `issues` table (e.g. closed >7d ago and pruned).
///
/// On success, also upserts the row into the local `issues` table so the
/// fetch becomes durable — the next `load_data` finds it and we don't repeat
/// the gh call on the next session. The upsert uses a writer connection with
/// busy_timeout=5s, the same pattern as the purge/test-verdict writers.
pub(crate) fn spawn_issue_fetch(
    repo_slug: String,
    repo_name: String,
    number: u64,
) -> std::sync::mpsc::Receiver<Result<FetchedIssue, String>> {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let output = std::process::Command::new("gh")
            .args([
                "issue",
                "view",
                &number.to_string(),
                "--repo",
                &repo_slug,
                "--json",
                "number,title,body,labels,state,milestone",
            ])
            .output();
        let result = match output {
            Ok(o) if o.status.success() => {
                match serde_json::from_slice::<serde_json::Value>(&o.stdout) {
                    Ok(v) => {
                        let labels: Vec<String> = v
                            .get("labels")
                            .and_then(|l| l.as_array())
                            .map(|arr| {
                                arr.iter()
                                    .filter_map(|l| {
                                        l.get("name").and_then(|n| n.as_str()).map(String::from)
                                    })
                                    .collect()
                            })
                            .unwrap_or_default();
                        // #406: parse milestone {number, title} or null.
                        let milestone_obj = v.get("milestone");
                        let milestone_number = milestone_obj
                            .and_then(|m| m.get("number"))
                            .and_then(|n| n.as_i64());
                        let milestone_title = milestone_obj
                            .and_then(|m| m.get("title"))
                            .and_then(|t| t.as_str())
                            .map(String::from);
                        let issue = FetchedIssue {
                            number,
                            title: v
                                .get("title")
                                .and_then(|s| s.as_str())
                                .unwrap_or("")
                                .to_string(),
                            body: v
                                .get("body")
                                .and_then(|s| s.as_str())
                                .unwrap_or("")
                                .to_string(),
                            labels,
                            state: v
                                .get("state")
                                .and_then(|s| s.as_str())
                                .unwrap_or("open")
                                .to_ascii_lowercase(),
                            milestone_number,
                            milestone_title,
                        };
                        // Best-effort upsert into the local DB. Failures (DB
                        // locked, schema missing, etc.) are non-fatal — the
                        // in-memory cache still serves the body for the rest
                        // of the session.
                        let _ = upsert_issue_db(&repo_name, &issue);
                        Ok(issue)
                    }
                    Err(e) => Err(format!("gh json parse failed: {}", e)),
                }
            }
            Ok(o) => Err(String::from_utf8_lossy(&o.stderr).trim().to_string()),
            Err(e) => Err(format!("could not run gh: {}", e)),
        };
        let _ = tx.send(result);
    });
    rx
}

/// #271 part 2 follow-up: spawn a background `gh pr view` to fetch the
/// PR title, body, and files-changed list for a single PR.  Mirrors
/// `spawn_issue_fetch`: same channel-receiver shape, same lifecycle in
/// the caching maps on `CoordApp` (`pending_pr_fetches` →
/// `fetched_prs_cache`).
pub(crate) fn spawn_pr_fetch(
    repo_slug: String,
    pr_number: i64,
) -> std::sync::mpsc::Receiver<Result<FetchedPr, String>> {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let output = std::process::Command::new("gh")
            .args([
                "pr",
                "view",
                &pr_number.to_string(),
                "--repo",
                &repo_slug,
                "--json",
                "title,body,files,reviews",
            ])
            .output();
        let result = match output {
            Ok(o) if o.status.success() => {
                match serde_json::from_slice::<serde_json::Value>(&o.stdout) {
                    Ok(v) => {
                        let files: Vec<String> = v
                            .get("files")
                            .and_then(|f| f.as_array())
                            .map(|arr| {
                                arr.iter()
                                    .filter_map(|f| {
                                        f.get("path").and_then(|n| n.as_str()).map(String::from)
                                    })
                                    .collect()
                            })
                            .unwrap_or_default();
                        let reviews: Vec<FetchedReview> = v
                            .get("reviews")
                            .and_then(|r| r.as_array())
                            .map(|arr| {
                                arr.iter()
                                    .map(|r| FetchedReview {
                                        state: r
                                            .get("state")
                                            .and_then(|s| s.as_str())
                                            .unwrap_or("")
                                            .to_string(),
                                        body: r
                                            .get("body")
                                            .and_then(|s| s.as_str())
                                            .unwrap_or("")
                                            .to_string(),
                                    })
                                    .collect()
                            })
                            .unwrap_or_default();
                        Ok(FetchedPr {
                            title: v
                                .get("title")
                                .and_then(|s| s.as_str())
                                .unwrap_or("")
                                .to_string(),
                            body: v
                                .get("body")
                                .and_then(|s| s.as_str())
                                .unwrap_or("")
                                .to_string(),
                            files,
                            reviews,
                        })
                    }
                    Err(e) => Err(format!("gh json parse failed: {}", e)),
                }
            }
            Ok(o) => Err(String::from_utf8_lossy(&o.stderr).trim().to_string()),
            Err(e) => Err(format!("could not run gh: {}", e)),
        };
        let _ = tx.send(result);
    });
    rx
}


/// Upsert a freshly-fetched issue into the local `issues` table. Mirrors the
/// `upsert_open_issues` Python helper but for a single row, using the same
/// connection-with-busy-timeout pattern as the other TUI writers (purge,
/// test-verdict). Single-statement transaction, safe under concurrent
/// coord/TUI writers per SQLite WAL semantics.
pub(crate) fn upsert_issue_db(repo_name: &str, issue: &FetchedIssue) -> rusqlite::Result<()> {
    let conn = open_purge_conn()?;
    let labels_json = serde_json::to_string(&issue.labels).unwrap_or_else(|_| "[]".to_string());
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64();
    conn.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at, \
         milestone_number, milestone_title) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9) \
         ON CONFLICT(repo_name, number) DO UPDATE SET \
            title = excluded.title, \
            body = excluded.body, \
            state = excluded.state, \
            labels = excluded.labels, \
            synced_at = excluded.synced_at, \
            milestone_number = excluded.milestone_number, \
            milestone_title = excluded.milestone_title",
        rusqlite::params![
            repo_name,
            issue.number as i64,
            issue.title,
            issue.body,
            issue.state,
            labels_json,
            now,
            issue.milestone_number,
            issue.milestone_title
        ],
    )?;
    Ok(())
}

/// Spawn a background thread that opens a Server-Sent Events connection to
/// `http://{host}:7433/stream/{id}`, parses SSE events, and sends them over
/// the returned `Receiver`.
///
/// ## Resume support
/// Pass `last_event_id > 0` to resume from a previous byte-offset by sending
/// the `Last-Event-Id` header.  The agent's `/stream/{id}` endpoint uses the
/// byte offset as the event id, so the stream resumes from that position.
///
/// ## Cancellation
/// Drop the returned `Receiver` to signal the thread to exit.  The thread
/// detects this on the next `tx.send()` call (which returns `Err`).  Under
/// normal conditions this happens within 15 s (SSE keepalive interval); a
/// 20-second read timeout acts as a safety net if keepalives stop.
pub(crate) fn spawn_sse_watch(
    host: &str,
    id: &str,
    last_event_id: u64,
) -> std::sync::mpsc::Receiver<SseWatchMsg> {
    let (tx, rx) = std::sync::mpsc::channel();
    let url = format!("http://{}:7433/stream/{}", host, id);
    std::thread::spawn(move || {
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(std::time::Duration::from_secs(5))
            // 20 s read timeout. The server sends SSE keepalives every 15 s so
            // this fires only when the connection is genuinely dead.
            .timeout_read(std::time::Duration::from_secs(20))
            .build();

        let mut builder = agent.get(&url);
        if last_event_id > 0 {
            builder = builder.set("Last-Event-Id", &last_event_id.to_string());
        }

        let resp = match builder.call() {
            Ok(r) => r,
            Err(e) => {
                let _ = tx.send(SseWatchMsg::Error(e.to_string()));
                return;
            }
        };

        use std::io::BufRead;
        let reader = std::io::BufReader::new(resp.into_reader());

        let mut current_id = last_event_id;
        let mut current_event = String::new();
        let mut current_data: Vec<String> = Vec::new();

        for line_result in reader.lines() {
            let line = match line_result {
                Ok(l) => l,
                Err(e) => {
                    // Read error (timeout, connection reset, etc.).
                    let _ = tx.send(SseWatchMsg::Error(e.to_string()));
                    return;
                }
            };

            // Empty line = dispatch the current accumulated event.
            if line.is_empty() {
                if !current_event.is_empty() || !current_data.is_empty() {
                    let text = current_data.join("\n");
                    let keep_going = match current_event.as_str() {
                        "log" => tx
                            .send(SseWatchMsg::Lines {
                                last_id: current_id,
                                text,
                            })
                            .is_ok(),
                        "end" => {
                            let _ = tx.send(SseWatchMsg::Done {
                                last_id: current_id,
                            });
                            return;
                        }
                        _ => true, // unknown event type — ignore
                    };
                    if !keep_going {
                        return; // receiver was dropped; exit cleanly
                    }
                    current_event.clear();
                    current_data.clear();
                }
                continue;
            }

            // SSE comment / keepalive — send a Heartbeat so the thread
            // discovers a dropped receiver (cancel) within one keepalive period.
            if line.starts_with(':') {
                if tx.send(SseWatchMsg::Heartbeat).is_err() {
                    return;
                }
                continue;
            }

            // SSE field lines.
            if let Some(v) = line.strip_prefix("id: ") {
                current_id = v.trim().parse().unwrap_or(current_id);
            } else if let Some(v) = line.strip_prefix("event: ") {
                current_event = v.trim().to_string();
            } else if let Some(v) = line.strip_prefix("data: ") {
                current_data.push(v.to_string());
            }
            // retry: lines are ignored — the main thread owns reconnect logic.
        }

        // EOF: connection closed without an explicit `end` event.
        let _ = tx.send(SseWatchMsg::Done {
            last_id: current_id,
        });
    });
    rx
}

/// Build a placeholder `WatchSseState` for assignments on the local machine
/// (no host ⇒ no SSE endpoint). The state starts as `done` so the watch
/// overlay falls back to the polling path without showing "Connecting…".
pub(crate) fn make_local_sse_state(_assignment_id: &str) -> WatchSseState {
    // Create a disconnected channel — we'll never use the receiver for real data.
    let (_tx, rx) = std::sync::mpsc::channel::<SseWatchMsg>();
    WatchSseState {
        rx,
        lines: Vec::new(),
        last_event_id: 0,
        fail_count: 0,
        first_fail_at: None,
        done: true, // Treat as done so the log fallback path is used.
        host: String::new(),
        pending_tail: String::new(),
        line_times: Vec::new(),
        current_turn: 0,
    }
}

/// Read pipeline-related entries from the `board_meta` table.
///
/// Returns ``(default_gates, tracked_labels, repos, require_plan,
/// repo_run_cmds, repo_paths)`` with the documented fallbacks when the keys
/// are missing or unparseable: gates default to ``["review", "merge"]``,
/// tracked labels to ``["coord"]``, repos to an empty list, require_plan to
/// ``false``, repo_run_cmds to an empty map, and repo_paths to an empty map.
/// Repos are returned as ``(coord_name, github_slug)`` pairs preserving
/// insertion order.
pub(crate) fn load_pipeline_meta(
    conn: &Connection,
) -> (
    Vec<String>,
    Vec<String>,
    Vec<(String, String)>,
    bool,
    std::collections::HashMap<String, String>,
    std::collections::HashMap<String, String>,
    Option<PipelineModels>,
    std::collections::HashMap<String, Vec<String>>,
) {
    fn read_key(conn: &Connection, key: &str) -> Option<String> {
        conn.query_row(
            "SELECT value FROM board_meta WHERE key = ?1",
            [key],
            |row| row.get::<_, String>(0),
        )
        .ok()
    }

    fn read_map(conn: &Connection, key: &str) -> std::collections::HashMap<String, String> {
        read_key(conn, key)
            .and_then(|v| serde_json::from_str::<serde_json::Value>(&v).ok())
            .and_then(|val| match val {
                serde_json::Value::Object(map) => Some(
                    map.into_iter()
                        .filter_map(|(k, v)| v.as_str().map(|s| (k, s.to_string())))
                        .collect(),
                ),
                _ => None,
            })
            .unwrap_or_default()
    }

    // #1151: repo_name -> list of route `match` globs (only present for
    // repos with a routed acceptance driver, #1125).
    fn read_map_of_lists(
        conn: &Connection,
        key: &str,
    ) -> std::collections::HashMap<String, Vec<String>> {
        read_key(conn, key)
            .and_then(|v| serde_json::from_str::<serde_json::Value>(&v).ok())
            .and_then(|val| match val {
                serde_json::Value::Object(map) => Some(
                    map.into_iter()
                        .filter_map(|(k, v)| {
                            let list = v.as_array()?;
                            let strs: Vec<String> = list
                                .iter()
                                .filter_map(|e| e.as_str().map(str::to_string))
                                .collect();
                            Some((k, strs))
                        })
                        .collect(),
                ),
                _ => None,
            })
            .unwrap_or_default()
    }

    let default_gates: Vec<String> = read_key(conn, "pipeline_default_gates")
        .and_then(|v| serde_json::from_str::<Vec<String>>(&v).ok())
        .unwrap_or_else(|| vec!["review".to_string(), "merge".to_string()]);

    let tracked_labels: Vec<String> = read_key(conn, "pipeline_tracked_labels")
        .and_then(|v| serde_json::from_str::<Vec<String>>(&v).ok())
        .unwrap_or_else(|| vec!["coord".to_string()]);

    let repos: Vec<(String, String)> = read_key(conn, "pipeline_repos")
        .and_then(|v| serde_json::from_str::<serde_json::Value>(&v).ok())
        .and_then(|val| match val {
            serde_json::Value::Object(map) => Some(
                map.into_iter()
                    .filter_map(|(k, v)| v.as_str().map(|s| (k, s.to_string())))
                    .collect(),
            ),
            _ => None,
        })
        .unwrap_or_default();

    let require_plan: bool = read_key(conn, "pipeline_require_plan")
        .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
        .unwrap_or(false);

    // #296: repo_name → run_cmd map from coordinator.yml.
    let repo_run_cmds = read_map(conn, "pipeline_repo_run_cmds");

    // #349: repo_name → local checkout path on this machine.
    let repo_paths = read_map(conn, "pipeline_repo_paths");

    // #803: model config snapshot for interactive --fix-of escalation.
    let pipeline_models: Option<PipelineModels> = read_key(conn, "pipeline_models")
        .and_then(|v| serde_json::from_str::<PipelineModels>(&v).ok());

    // #1151: repo_name → route match globs, for repos with a routed
    // acceptance driver.
    let acceptance_routes = read_map_of_lists(conn, "pipeline_acceptance_routes");

    (
        default_gates,
        tracked_labels,
        repos,
        require_plan,
        repo_run_cmds,
        repo_paths,
        pipeline_models,
        acceptance_routes,
    )
}

/// Open a writer connection with a 5s busy timeout so a brief lock from
/// the coordinator doesn't make purge silently no-op.
pub(crate) fn open_purge_conn() -> rusqlite::Result<Connection> {
    let db_path = coord_dir().join("coord.db");
    let conn = Connection::open(&db_path)?;
    conn.busy_timeout(Duration::from_millis(5000))?;
    Ok(conn)
}
