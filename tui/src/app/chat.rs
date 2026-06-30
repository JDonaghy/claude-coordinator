#[allow(unused_imports)]
use super::*;


/// Incremental core of [`parse_sse_log_readable`].
///
/// Processes `new_lines`/`new_times` starting from `state`, mutating `state`
/// to reflect the newly consumed lines so subsequent calls continue from where
/// this one stopped.  The caller is responsible for slicing off only the lines
/// that have not yet been processed.
///
/// This is the function [`LogItemsCache`] calls when new SSE lines arrive:
/// instead of re-parsing thousands of accumulated lines from scratch, it parses
/// only the lines appended since the last cache fill — O(new_lines) rather than
/// O(total_lines).
///
/// # Correctness of incremental pre-pass
///
/// `user_epoch_per_turn` is a forward-only scan: it tracks `last_user_epoch`
/// and appends an entry per `"type":"assistant"` event.  Because lines are
/// append-only (SSE streams never prepend or reorder), extending the pre-pass
/// with only the new suffix is equivalent to running it over all lines — earlier
/// entries in `state.user_epochs` never change.
pub(crate) fn parse_sse_log_more(
    new_lines: &[String],
    new_times: &[std::time::Instant],
    wrap_width: usize,
    state: &mut LogParseState,
) -> Vec<ListItem> {
    // Incremental pre-pass: extend user_epochs using only the new lines,
    // starting from the last user epoch seen in prior calls.
    for line in new_lines {
        match json_str(line, "type").as_deref() {
            Some("user") => {
                if let Some(ts) = json_str(line, "timestamp") {
                    state.last_user_epoch = parse_iso8601_to_epoch(&ts);
                }
            }
            Some("assistant") => {
                state.user_epochs.push(state.last_user_epoch);
            }
            _ => {}
        }
    }

    let mut items = Vec::new();
    for (i, line) in new_lines.iter().enumerate() {
        let t = new_times.get(i).copied();
        let elapsed = if json_str(line, "type").as_deref() == Some("assistant") {
            // Prefer content-embedded user timestamps (survive replay bursts).
            // Fall back to arrival-Instant for turns with no preceding user
            // event (typically the very first inter-turn gap on a live stream).
            let e = user_epoch_elapsed(&state.user_epochs, state.assistant_idx).or_else(|| {
                t.and_then(|now| {
                    state
                        .last_assistant_time
                        .map(|prev| now.duration_since(prev))
                })
            });
            state.last_assistant_time = t;
            state.assistant_idx += 1;
            e
        } else {
            None
        };
        items.extend(parse_json_events_readable(
            line,
            &mut state.turn_n,
            elapsed,
            wrap_width,
        ));
        // Surface structured review verdict after result events.
        if line.contains("\"type\":\"result\"") {
            items.extend(extract_review_items(line));
        }
    }
    items
}

/// Render SSE log lines using the readable (#385) format: wrapped prose,
/// arrow-prefixed tool calls, one line per tool call, thinking on separate
/// wrapped lines.  Used by both the Log tab and the live watch overlay.
/// `wrap_width == 0` disables wrapping.
///
/// Inter-turn `+Ns` timing is derived from `user.timestamp` fields embedded in
/// the stream-json (fix for #309: byte-0 SSE replay collapses all arrival
/// `Instant`s to ~now, so timing must come from the log content itself).
/// Arrival-`Instant` deltas are used as fallback for turns that have no
/// preceding user event (e.g. the first inter-turn gap on live streams before
/// the second user event arrives).
///
/// Delegates to [`parse_sse_log_more`] from a fresh [`LogParseState`] so that
/// all callers (watch overlay, tests) are unaffected by the refactor.
pub(crate) fn parse_sse_log_readable(
    lines: &[String],
    times: &[std::time::Instant],
    wrap_width: usize,
) -> Vec<ListItem> {
    let mut state = LogParseState::default();
    parse_sse_log_more(lines, times, wrap_width, &mut state)
}

/// Like `parse_log_content` but renders using the readable (#385) format.
/// Used for file-based logs shown in the Pipeline Log tab.
///
/// Timing for `+Ns` labels is derived from `user.timestamp` fields in the
/// stream-json (same fix as `parse_sse_log_readable` for #309).
pub(crate) fn parse_log_content_readable(content: &str, wrap_width: usize) -> Vec<ListItem> {
    let is_json = content
        .lines()
        .find(|l| !l.starts_with('#') && !l.trim().is_empty())
        .map(|l| l.trim_start().starts_with('{'))
        .unwrap_or(false);

    // Pre-pass: extract per-turn user timestamps for timing labels.
    let user_epochs = if is_json {
        user_epoch_per_turn(content.lines())
    } else {
        Vec::new()
    };

    let mut items: Vec<ListItem> = Vec::new();
    let mut turn_n: usize = 0;
    let mut assistant_idx: usize = 0;

    for line in content.lines() {
        if line.starts_with('#') || line.trim().is_empty() {
            continue;
        }
        if is_json {
            let elapsed = if json_str(line, "type").as_deref() == Some("assistant") {
                let e = user_epoch_elapsed(&user_epochs, assistant_idx);
                assistant_idx += 1;
                e
            } else {
                None
            };
            items.extend(parse_json_events_readable(
                line,
                &mut turn_n,
                elapsed,
                wrap_width,
            ));
            if line.contains("\"type\":\"result\"") {
                items.extend(extract_review_items(line));
            }
        } else {
            // Plain-text log: surface STATUS: / STUCK: lines.
            if line.contains("STATUS:") {
                if let Some(idx) = line.find("STATUS:") {
                    let rest = line[idx..].trim();
                    items.push(activity_item(rest, Color::rgb(80, 210, 80)));
                }
            } else if line.contains("STUCK:") {
                if let Some(idx) = line.find("STUCK:") {
                    let rest = line[idx..].trim();
                    items.push(activity_item(rest, Color::rgb(220, 120, 50)));
                }
            }
        }
    }

    if items.is_empty() {
        items.push(kv_item(
            "",
            "  No activity yet",
            Some(Color::rgb(100, 100, 100)),
        ));
    }
    items
}

/// #319 Phase A: walk the focused chat's SSE log from `floor` forward,
/// concatenating assistant `text` blocks into the proposed comment body.
/// Uses [`extract_text_block_keep_newlines`] so the markdown structure
/// (headings, lists, code fences) survives the trip into the
/// `gh issue comment` body.  An empty result means the assistant exited
/// without emitting any text.
pub(crate) fn extract_assistant_text_after(ctx: &WatchContext, floor: usize) -> String {
    let mut out = String::new();
    for line in ctx.sse.lines.iter().skip(floor) {
        if json_str(line, "type").as_deref() != Some("assistant") {
            continue;
        }
        let text = extract_text_block_keep_newlines(line);
        let body = text.trim();
        if body.is_empty() {
            continue;
        }
        if !out.is_empty() {
            out.push_str("\n\n");
        }
        out.push_str(body);
    }
    out
}


pub(crate) fn chat_transcript_from_pool(ctx: &WatchContext) -> Vec<ChatTurn> {
    let mut turns: Vec<ChatTurn> = Vec::new();
    // #315: if this context inherited frozen turns from a prior worker in
    // the same chat session (via `maybe_bind_pending_resume`), render them
    // first.  The history itself is the seed for the rebound chat, so we
    // skip the synthetic System seed in that case.
    if !ctx.history_turns.is_empty() {
        turns.extend(ctx.history_turns.iter().cloned());
    } else if ctx.state.assignment_type == "refinement" {
        turns.push(ChatTurn {
            role: ChatRole::System,
            text: StyledText::plain(format!(
                "Refinement chat seeded with #{}'s issue body + CLAUDE.md + repo file tree. \
                 The assistant has read all of that; ask it questions about scope.",
                ctx.state.issue_number
            )),
            timestamp_unix: None,
            line_scales: Vec::new(),
        });
    }
    let mut user_idx = 0usize;
    for (sse_idx, line) in ctx.sse.lines.iter().enumerate() {
        // Emit any user turns submitted at or before this SSE position.
        while user_idx < ctx.inject_transcript.len()
            && ctx.inject_sse_offsets.get(user_idx).copied().unwrap_or(0) <= sse_idx
        {
            turns.push(ctx.inject_transcript[user_idx].clone());
            user_idx += 1;
        }
        if json_str(line, "type").as_deref() != Some("assistant") {
            continue;
        }
        // #372 follow-up: keep newlines so the markdown adapter can see block
        // structure (lists, headings, fenced code, blockquotes). The plain
        // extract_text_block flattens \n→space, which left only inline markdown
        // (bold/italic) rendering.
        let text = extract_text_block_keep_newlines(line);
        let body = text.trim();
        if body.is_empty() {
            continue;
        }
        // Render the whole assistant body through quadraui's markdown adapter
        // so headings, bold, italic, inline code, lists, blockquotes, and
        // fenced code blocks are styled.
        // TODO(#217): thread the active_theme through to here once
        // chat_transcript_from_pool accepts a theme parameter; for now the
        // quadraui dark default is close enough for the Dark palette.
        let md_theme = quadraui::Theme::default();
        let rendered = quadraui::render_markdown_to_styled(body, &md_theme);
        let mut md_spans: Vec<StyledSpan> = Vec::new();
        for (i, md_line) in rendered.lines.into_iter().enumerate() {
            if i > 0 {
                md_spans.push(StyledSpan::plain("\n"));
            }
            md_spans.extend(md_line.spans);
        }
        turns.push(ChatTurn {
            role: ChatRole::Assistant,
            text: StyledText { spans: md_spans },
            timestamp_unix: None,
            line_scales: Vec::new(),
        });
    }
    // Tail: any user turns submitted *after* the last SSE line we've seen
    // (assistant hasn't replied yet) still belong in the transcript.
    while user_idx < ctx.inject_transcript.len() {
        turns.push(ctx.inject_transcript[user_idx].clone());
        user_idx += 1;
    }
    turns
}
