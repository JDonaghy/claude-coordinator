#!/usr/bin/env bash
#
# run_tests_in_populated_home.sh — the #2170 regression check: run a command
# (default: the full pytest suite) in the environment of a REAL FLEET MACHINE
# rather than of a fresh CI runner.
#
#   scripts/run_tests_in_populated_home.sh [command...]
#
# WHY THIS EXISTS. Six tests failed on `origin/main` on every fleet machine
# and passed on `ubuntu-latest` across 3.12 and 3.13. The consequence was not
# six red tests: it was that the **Test stage on `precision` could not produce
# a green verdict for this repo on any branch** — every dispatch there returned
# `SMOKE: fail`, blamed the branch, and cost a human verdict to adjudicate.
#
# The direction is what made it survive. The familiar version of this bug is
# "passes on my machine, fails in CI ⇒ something ambient in $HOME". These were
# the INVERSE: they passed in CI *precisely because* CI's $HOME is empty. A
# green ubuntu-latest matrix is structurally incapable of catching that, because
# the only environment it ever tests is the empty one. So the fix has to include
# a way to run the suite in the *populated* environment — that is this script,
# and .github/workflows/test.yml's `populated-home` job which invokes it.
#
# THE THREE KNOBS, each of which broke exactly one of the six:
#
#  1. A POPULATED ~/.coord/. `$HOME` is redirected to a throwaway directory
#     seeded with `client.toml` (so `coord` is a THIN CLIENT) and a
#     `coordinator.remote.yml` cache, and deliberately NO `coordinator.yml` —
#     `precision`'s exact shape. On a thin client `coord config --config <file>`
#     does not read the file you hand it; it re-fetches `GET /config` from the
#     daemon and parses that (CLAUDE.md: "On a thin client, that resolved path
#     is a CACHE, not the config"; docs/EPHEMERAL_WORKERS.md: "`coord config
#     --config` is not a validator on a thin client"). The daemon URL points at
#     127.0.0.1:9 (the IANA discard port): unreachable, so nothing leaves the
#     machine and no daemon need be running, while the thin-client branch is
#     still taken.
#
#  2. NO `sqlite3` ON $PATH. It is absent on `precision`, and present on
#     `elitebook` only incidentally via the Android SDK's platform-tools — it
#     is not a provisioned fleet dependency anywhere, and `pyproject.toml`'s
#     `[dev]` extras cannot install a system binary. Masked by NAME, using the
#     same shadow-directory technique as `run_tests_without_gh.sh` (#1484) and
#     for the same reason: `PATH=/usr/bin:/bin` would be wrong on hosts where
#     the binary lives elsewhere, and dropping whichever directory contains it
#     would usually mean dropping /usr/bin — taking bash and git with it, and
#     producing failures that have nothing to do with the guard under test.
#
#  3. A $TMPDIR UNDER AN ANCESTOR PYTEST CONFIG. pytest infers rootdir by
#     walking UPWARD for pytest.ini / pyproject.toml / tox.ini / setup.cfg, and
#     derives each JUnit `classname` from the nodeid relative to rootdir. A
#     nested pytest run inside a `tmp_path` whose ancestors carry one of those
#     therefore reports `inner.test_sample::test_pass`, not
#     `test_sample::test_pass`. Reproduced directly; this is the mechanism, not
#     a hypothesis.
#
# `tests/test_ambient_home_isolation.py` builds the same three knobs in Python
# and pins the specific affected targets on EVERY run, everywhere — that is the
# always-on gate. This script is the whole-suite sweep: the thing you run to
# find the NEXT test of this class before a fleet machine does.
#
# Exit codes: whatever the wrapped command exits with; 2 if a knob failed to
# take effect (the guard itself is broken — fail loud rather than report a
# green that means nothing, which is the failure mode #2170 is about).

set -euo pipefail

MASKED_BINARY="sqlite3"

_scratch="$(mktemp -d)"
trap 'rm -rf "$_scratch"' EXIT

# ── knob 1: a populated ~/.coord/, thin-client shaped ────────────────────────

_home="$_scratch/home"
mkdir -p "$_home/.coord"
cat >"$_home/.coord/client.toml" <<'TOML'
board_service = "http://127.0.0.1:9"
TOML
cat >"$_home/.coord/coordinator.remote.yml" <<'YAML'
repos:
  - name: fleet-only-repo
    github: fleet/only
machines:
  - name: fleet-only-machine
    host: fleet-only.tailnet
    repos: [fleet-only-repo]
YAML
# No coordinator.yml, on purpose — see knob 1 in the header.

# ── knob 3: a $TMPDIR whose ancestor carries a pytest config ─────────────────

cat >"$_home/pyproject.toml" <<'TOML'
[tool.pytest.ini_options]
testpaths = ["tests"]
TOML
mkdir -p "$_home/tmp"

# ── knob 2: mask one binary NAME off $PATH, wherever it lives ────────────────

_new_path=""
_i=0
IFS=':' read -ra _dirs <<< "$PATH"
for d in "${_dirs[@]}"; do
    if [[ -d "$d" && -e "$d/$MASKED_BINARY" ]]; then
        _i=$((_i + 1))
        shadow="$_scratch/shadow-$_i"
        mkdir -p "$shadow"
        for f in "$d"/*; do
            [[ -e "$f" ]] || continue
            name="$(basename "$f")"
            [[ "$name" == "$MASKED_BINARY" ]] && continue
            ln -s "$f" "$shadow/$name" 2>/dev/null || true
        done
        d="$shadow"
    fi
    _new_path="${_new_path:+$_new_path:}$d"
done

export PATH="$_new_path"
export HOME="$_home"
export USERPROFILE="$_home"
export TMPDIR="$_home/tmp"
# The bootstrap contract is flag > env > file, and knob 1 wants the FILE to be
# what makes this a thin client (that is `precision`'s shape, and it means the
# code under test has to isolate $HOME, not merely scrub two env vars).
unset COORD_SERVICE_URL COORD_TOKEN COORD_CONFIG

# ── the guard must actually guard ────────────────────────────────────────────

if command -v "$MASKED_BINARY" >/dev/null 2>&1; then
    echo "run_tests_in_populated_home.sh: $MASKED_BINARY is STILL resolvable at $(command -v "$MASKED_BINARY") after exclusion — the guard itself is broken" >&2
    exit 2
fi
for required in bash sh git; do
    if ! command -v "$required" >/dev/null 2>&1; then
        echo "run_tests_in_populated_home.sh: masking took '$required' with it — the guard is too broad" >&2
        exit 2
    fi
done
if [[ ! -f "$HOME/.coord/client.toml" || -e "$HOME/.coord/coordinator.yml" ]]; then
    echo "run_tests_in_populated_home.sh: the seeded \$HOME is not thin-client shaped" >&2
    exit 2
fi

echo "run_tests_in_populated_home.sh: HOME=$HOME (thin client, no coordinator.yml), TMPDIR=$TMPDIR, no $MASKED_BINARY on PATH" >&2

if [[ $# -eq 0 ]]; then
    set -- python -m pytest tests/ -q
fi

exec "$@"
