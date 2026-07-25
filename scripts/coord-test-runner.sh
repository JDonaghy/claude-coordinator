#!/usr/bin/env bash
#
# coord-test-runner.sh — run the RIGHT tests for a claude-coordinator branch,
# in a throwaway worktree, and distinguish a real failure from a flake.
#
#   scripts/coord-test-runner.sh <worktree> [--base-ref REF] [--report FILE]
#
# This is the Test gate's engine.  `drive-issue.sh` calls it; it is also useful
# on its own ("did this branch actually break anything?").
#
# Three things it handles that a bare `pytest && cargo test` does not:
#
#  1. PATH ROUTING.  This repo is two codebases with two toolchains, and a
#     single `test_command` in coordinator.yml cannot express that.  Changes
#     under coord/** or tests/** run pytest; changes under tui/** run
#     `cargo test`.  A docs-only diff runs neither and reports SKIP.
#     This is not just a speed optimisation — running the Rust suite against a
#     pure-Python diff adds flake risk for zero signal (observed: #1349's
#     branch, a Python-only change, tripped a tui flake).
#
#  2. THE quadraui PATH DEP.  tui/Cargo.toml points at
#     `../../quadraui/quadraui`, which is resolved RELATIVE TO THE WORKTREE —
#     so in a scratch worktree it dangles and the build fails outright.  A
#     sibling symlink to the real checkout is required.  Verified: without it
#     the path does not exist; with it, the build succeeds.
#
#  3. FLAKE FILTERING.  The tui suite has known races under full-parallel
#     `cargo test` (#1260 tracks 3 in commands::tests; this script also caught
#     app::tests::plans_panel_capture_key_dispatches_milestone_capture, which
#     is NOT in that issue — so the set is larger than filed).  On failure we
#     re-run ONLY the failed tests, serially and isolated.  If they pass, the
#     run is reported as a flake-tolerated PASS rather than burning an
#     escalated fix round on a test the worker never touched.
#     Build/collection errors are never flake-retried — those are always real.
#
# Exit codes: 0 pass (or skip — nothing to test), 1 genuine failure, 2 usage.

set -euo pipefail

BASE_REF="origin/main"
REPORT=""
QUADRAUI_SRC="${QUADRAUI_SRC:-$HOME/src/quadraui}"
# Persistent so the 3m12s cold Rust build is paid once, not once per fix round
# (warm rebuilds land in 10-35s).
CARGO_TARGET="${COORD_TEST_CARGO_TARGET:-${TMPDIR:-/tmp}/coord-test-cargo-target-$(id -u)}"

WT=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --base-ref) BASE_REF="$2"; shift 2 ;;
        --report)   REPORT="$2"; shift 2 ;;
        -h|--help)  sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*)         echo "unknown option: $1" >&2; exit 2 ;;
        *)          WT="$1"; shift ;;
    esac
done

[[ -n "$WT" && -d "$WT" ]] || { echo "usage: $0 <worktree> [--base-ref REF]" >&2; exit 2; }
WT="$(cd "$WT" && pwd)"

log()  { printf '    [test] %s\n' "$*"; }
warn() { printf '    [test] !! %s\n' "$*" >&2; }

say() { [[ -n "$REPORT" ]] && printf '%s\n' "$*" >>"$REPORT"; printf '%s\n' "$*"; }
[[ -n "$REPORT" ]] && : >"$REPORT"

# ── what changed? ────────────────────────────────────────────────────────────

if ! CHANGED="$(git -C "$WT" diff --name-only "${BASE_REF}...HEAD" 2>/dev/null)"; then
    warn "could not diff against $BASE_REF — falling back to running everything"
    CHANGED=""
    RUN_PY=1; RUN_RS=1
else
    RUN_PY=0; RUN_RS=0
    while IFS= read -r f; do
        [[ -z "$f" ]] && continue
        case "$f" in
            coord/*|tests/*|pyproject.toml|conftest.py) RUN_PY=1 ;;
            tui/*)                                      RUN_RS=1 ;;
        esac
    done <<<"$CHANGED"
fi

n_changed="$(printf '%s\n' "$CHANGED" | grep -c . || true)"
log "changed files vs $BASE_REF: $n_changed"
log "routing: pytest=$RUN_PY cargo=$RUN_RS"

if [[ "$RUN_PY" -eq 0 && "$RUN_RS" -eq 0 ]]; then
    say "SKIP: no test-bearing paths changed (docs/config only)"
    exit 0
fi

FAILED_SUITES=()
FLAKES=()

# ── python ───────────────────────────────────────────────────────────────────

run_python() {
    local venv="$WT/.venv"
    if [[ ! -x "$venv/bin/python" ]]; then
        log "creating venv + installing .[dev] (~12s)"
        python3 -m venv "$venv" >/dev/null
        "$venv/bin/pip" install -q -e "$WT[dev]" >/dev/null 2>&1 || {
            say "FAIL(python): could not install .[dev] — environment problem, not a code failure"
            return 1
        }
    fi

    # Use xdist when the BRANCH's pyproject.toml pulls it in (~2.4x faster:
    # 5m49s → 1m36s on a 4642-test suite, identical results). Detected rather
    # than assumed, because the venv is built from the branch under test and
    # any branch predating the dev-dep would die on an unknown -n flag.
    local par=()
    if "$venv/bin/python" -c "import xdist" 2>/dev/null; then
        par=(-n auto)
        log "pytest-xdist present → running in parallel"
    fi

    local out="$WT/.pytest.out"
    log "running: pytest -q ${par[*]:-(serial)} (full suite)"
    # ${par[@]+...} so an empty array is not an unbound-variable error under
    # `set -u` on older bash.
    if (cd "$WT" && "$venv/bin/python" -m pytest -q --tb=short ${par[@]+"${par[@]}"}) >"$out" 2>&1; then
        say "PASS(python): $(grep -oE '[0-9]+ passed[^)]*' "$out" | tail -1)"
        return 0
    fi

    # A collection/import error is never a flake — the suite could not even run.
    if grep -qE "^(ERROR|INTERNALERROR)" "$out"; then
        say "FAIL(python): collection/import error"
        tail -n 30 "$out" | sed 's/^/      /'
        return 1
    fi

    local failed
    failed="$(grep '^FAILED ' "$out" | awk '{print $2}' | sort -u || true)"
    if [[ -z "$failed" ]]; then
        say "FAIL(python): non-zero exit with no parseable FAILED lines"
        tail -n 30 "$out" | sed 's/^/      /'
        return 1
    fi

    local count; count="$(printf '%s\n' "$failed" | grep -c . || true)"
    log "$count test(s) failed — re-running them in isolation to filter flakes"
    local rerun="$WT/.pytest.rerun.out"
    # shellcheck disable=SC2086  # node ids are intentionally word-split
    if (cd "$WT" && "$venv/bin/python" -m pytest -q --tb=short $failed) >"$rerun" 2>&1; then
        say "FLAKE(python): $count test(s) failed in the full run but PASS in isolation"
        printf '%s\n' "$failed" | sed 's/^/      /'
        FLAKES+=("python:$count")
        return 0
    fi

    say "FAIL(python): $count test(s) fail on re-run — genuine"
    printf '%s\n' "$failed" | sed 's/^/      /'
    tail -n 40 "$rerun" | sed 's/^/      /'
    return 1
}

# ── rust / coord-tui ─────────────────────────────────────────────────────────

run_rust() {
    # tui/Cargo.toml: quadraui = { path = "../../quadraui/quadraui" }, resolved
    # from tui/ — so the worktree needs a quadraui sibling or the build dies.
    local sibling
    sibling="$(dirname "$WT")/quadraui"
    if [[ ! -e "$sibling" ]]; then
        [[ -d "$QUADRAUI_SRC" ]] || {
            say "FAIL(rust): quadraui checkout not found at $QUADRAUI_SRC"
            return 1
        }
        log "linking $sibling → $QUADRAUI_SRC (path-dep resolution)"
        ln -sfn "$QUADRAUI_SRC" "$sibling"
    fi
    log "quadraui branch: $(git -C "$QUADRAUI_SRC" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"

    export CARGO_TARGET_DIR="$CARGO_TARGET"
    local out="$WT/.cargo.out"
    log "running: cargo test (cold ~3m, warm ~30s)"
    if (cd "$WT/tui" && cargo test) >"$out" 2>&1; then
        say "PASS(rust): $(grep -oE '[0-9]+ passed[^;]*' "$out" | head -1)"
        return 0
    fi

    # A compile error is never a flake.
    if grep -qE "^error(\[E[0-9]+\])?:|could not compile" "$out"; then
        say "FAIL(rust): compile error"
        grep -E "^error(\[E[0-9]+\])?:|^  -->" "$out" | head -n 20 | sed 's/^/      /'
        return 1
    fi

    # The `failures:` summary block lists bare test paths, one per line.
    local failed
    failed="$(awk '/^failures:$/{f=1;next} /^test result:/{f=0} f && /^    [a-zA-Z_]+::/{print $1}' "$out" | sort -u || true)"
    if [[ -z "$failed" ]]; then
        say "FAIL(rust): non-zero exit with no parseable failure list"
        tail -n 30 "$out" | sed 's/^/      /'
        return 1
    fi

    local count; count="$(printf '%s\n' "$failed" | grep -c . || true)"
    log "$count test(s) failed — re-running serially to filter the #1260-class races"
    local all_passed=1
    local rerun="$WT/.cargo.rerun.out"
    : >"$rerun"
    while IFS= read -r t; do
        [[ -z "$t" ]] && continue
        if ! (cd "$WT/tui" && cargo test "$t" -- --exact --test-threads=1) >>"$rerun" 2>&1; then
            all_passed=0
        fi
    done <<<"$failed"

    if [[ "$all_passed" -eq 1 ]]; then
        say "FLAKE(rust): $count test(s) failed under full parallelism but PASS isolated (#1260 class)"
        printf '%s\n' "$failed" | sed 's/^/      /'
        FLAKES+=("rust:$count")
        return 0
    fi

    say "FAIL(rust): $count test(s) fail on isolated re-run — genuine"
    printf '%s\n' "$failed" | sed 's/^/      /'
    tail -n 40 "$rerun" | sed 's/^/      /'
    return 1
}

# ── drive ────────────────────────────────────────────────────────────────────

if [[ "$RUN_PY" -eq 1 ]]; then
    run_python || FAILED_SUITES+=("python")
fi
if [[ "$RUN_RS" -eq 1 ]]; then
    run_rust || FAILED_SUITES+=("rust")
fi

if [[ ${#FLAKES[@]} -gt 0 ]]; then
    say "NOTE: flakes tolerated this run: ${FLAKES[*]} — see #1260"
fi

if [[ ${#FAILED_SUITES[@]} -gt 0 ]]; then
    say "RESULT: FAIL (${FAILED_SUITES[*]})"
    exit 1
fi
say "RESULT: PASS"
exit 0
