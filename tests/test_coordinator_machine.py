"""Tests for scripts/azure-workers/coordinator-machine.py (#1799).

#1799: `epic-up.sh` registered an ephemeral worker with no `repo_paths` and
no `provider:*` capability, so the machine it just paid to provision could
not receive a single dispatch — `coord.dispatch.dispatch` refuses with "No
repo_path configured" before it ever reaches the provider/TOS gates. Two
halves of the fix live here:

  1. `cmd_add` now derives `machines[].repo_paths` from `--repos` and a new
     `--repo-root` (default `~/src`, matching the golden image's layout),
     instead of emitting a machine entry that can never dispatch.
  2. `cmd_remove` must still cleanly remove an entry that now has this extra
     nested block — the regression risk the issue explicitly calls out.

The script is invoked as a subprocess exactly the way epic-up.sh calls it
(`--file`/`--out`, `add`/`remove`) rather than imported, since it is a
`#!/usr/bin/env python3` script with no `__init__.py`-having package to
import from.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from coord.config import load
from coord.providers import provider_capability

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "azure-workers" / "coordinator-machine.py"

BASE_CONFIG = """\
repos:
  - name: claude-coordinator
    github: acme/claude-coordinator
  - name: quadraui
    github: acme/quadraui
  - name: vimcode
    github: acme/vimcode

machines:
  - name: dellserver
    host: dellserver
    capabilities: [rust, python, browser, gtk, provider:opencode]
    repos: [claude-coordinator, quadraui, vimcode]
    repo_paths:
      claude-coordinator: ~/src/claude-coordinator
      quadraui: ~/src/quadraui
      vimcode: ~/src/vimcode
  # >>> epic-machines (managed by epic-up.sh) >>>
  # <<< epic-machines <<<
"""


def _run_helper(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def _add(tmp_path: Path, config_text: str = BASE_CONFIG, **extra_args: str) -> Path:
    """Write `config_text`, run `add` with sane defaults + `extra_args`
    layered on top, and return the path written to `--out`."""
    cfg = tmp_path / "coordinator.yml"
    cfg.write_text(config_text)
    out = tmp_path / "out.yml"
    args = [
        "--file", str(cfg), "--out", str(out), "add",
        "--name", "azure-epic1799", "--host", "azure-epic1799",
        "--capabilities", "rust,python",
        "--repos", "claude-coordinator,quadraui",
    ]
    for k, v in extra_args.items():
        args += [f"--{k.replace('_', '-')}", v]
    result = _run_helper(*args)
    assert result.returncode == 0, result.stderr
    return out


def test_add_emits_repo_paths_for_every_declared_repo(tmp_path: Path) -> None:
    """#1799 core fix: the generated entry must carry `repo_paths` for every
    repo in `--repos`, derived from the default `--repo-root` (`~/src`)."""
    out = _add(tmp_path)
    text = out.read_text()
    assert "repo_paths:" in text
    assert "claude-coordinator: ~/src/claude-coordinator" in text
    assert "quadraui: ~/src/quadraui" in text


def test_add_repo_paths_is_dispatchable_via_coord_config(tmp_path: Path) -> None:
    """#1799 acceptance: load the generated config with coord's OWN parser
    and confirm `Machine.repo_path()` — the exact call
    `coord.dispatch.dispatch()` makes before refusing with "No repo_path
    configured" — resolves for every repo epic-up.sh declared. This is the
    repo_path half of what a `coord assign --dry-run` would have caught."""
    out = _add(tmp_path)
    cfg = load(out)
    machine = next(m for m in cfg.machines if m.name == "azure-epic1799")
    assert machine.repo_path("claude-coordinator") == "~/src/claude-coordinator"
    assert machine.repo_path("quadraui") == "~/src/quadraui"


def test_add_without_repo_paths_is_not_dispatchable_today_script_regression(
    tmp_path: Path,
) -> None:
    """Named regression proof: the pre-#1799 shape (no `repo_paths` at all,
    exactly what today's `cmd_add` emitted before this fix) fails the same
    `repo_path()` check. This is a static demonstration of the old shape —
    it hand-builds the pre-fix YAML rather than invoking `cmd_add` against a
    prior revision of the script — not a true test-over-history regression,
    but it pins down why the fix in the test above matters."""
    cfg = tmp_path / "coordinator.yml"
    cfg.write_text(BASE_CONFIG)
    out = tmp_path / "out.yml"
    old_style = cfg.read_text().replace(
        "  # >>> epic-machines (managed by epic-up.sh) >>>\n",
        "  # >>> epic-machines (managed by epic-up.sh) >>>\n"
        "  - name: azure-epic1799\n"
        "    host: azure-epic1799\n"
        "    capabilities: [rust, python]\n"
        "    repos: [claude-coordinator, quadraui]\n",
    )
    out.write_text(old_style)
    loaded = load(out)
    machine = next(m for m in loaded.machines if m.name == "azure-epic1799")
    assert machine.repo_path("claude-coordinator") is None
    assert machine.repo_path("quadraui") is None


def test_add_repo_root_is_configurable(tmp_path: Path) -> None:
    out = _add(tmp_path, repo_root="/home/coord/src")
    text = out.read_text()
    assert "claude-coordinator: /home/coord/src/claude-coordinator" in text
    assert "quadraui: /home/coord/src/quadraui" in text


def test_add_repo_root_trailing_slash_does_not_double_up(tmp_path: Path) -> None:
    out = _add(tmp_path, repo_root="~/src/")
    text = out.read_text()
    assert "claude-coordinator: ~/src/claude-coordinator" in text
    assert "~/src//claude-coordinator" not in text


def test_add_repo_paths_reference_only_the_requested_repos(tmp_path: Path) -> None:
    """`--repos` may be a subset of the repos known to the config — only
    those should get a repo_paths entry, not every repo in the file."""
    cfg = tmp_path / "coordinator.yml"
    cfg.write_text(BASE_CONFIG)
    out = tmp_path / "out.yml"
    result = _run_helper(
        "--file", str(cfg), "--out", str(out), "add",
        "--name", "azure-epic1799", "--host", "azure-epic1799",
        "--capabilities", "rust,python",
        "--repos", "claude-coordinator",
    )
    assert result.returncode == 0, result.stderr
    loaded = load(out)
    machine = next(m for m in loaded.machines if m.name == "azure-epic1799")
    assert machine.repo_path("claude-coordinator") == "~/src/claude-coordinator"
    assert machine.repo_path("vimcode") is None  # not requested, not this machine's


def test_add_advertising_provider_opencode_satisfies_capability_check(
    tmp_path: Path,
) -> None:
    """#1799 acceptance: a machine registered with `provider:opencode` in
    `--capabilities` is recognized by `coord.providers.provider_capability`
    the same way the standing-fleet `dellserver` example in the issue is."""
    out = _add(tmp_path, capabilities="rust,python,provider:opencode")
    cfg = load(out)
    machine = next(m for m in cfg.machines if m.name == "azure-epic1799")
    assert provider_capability("opencode") in machine.capabilities


def test_remove_cleanly_removes_an_entry_that_now_has_repo_paths(
    tmp_path: Path,
) -> None:
    """Regression risk the issue calls out by name: `remove` must still
    cleanly delete an entry now that `add` emits an extra nested
    `repo_paths:` mapping — round-tripping back to the original file
    byte-for-byte."""
    added = _add(tmp_path)
    removed = tmp_path / "removed.yml"
    result = _run_helper(
        "--file", str(added), "--out", str(removed),
        "remove", "--name", "azure-epic1799",
    )
    assert result.returncode == 0, result.stderr
    assert removed.read_text() == BASE_CONFIG


def test_remove_of_absent_machine_is_idempotent(tmp_path: Path) -> None:
    cfg = tmp_path / "coordinator.yml"
    cfg.write_text(BASE_CONFIG)
    out = tmp_path / "out.yml"
    result = _run_helper(
        "--file", str(cfg), "--out", str(out),
        "remove", "--name", "nonexistent-machine",
    )
    assert result.returncode == 0, result.stderr
    assert "not present" in result.stderr
    assert out.read_text() == BASE_CONFIG


def test_add_twice_refuses_duplicate(tmp_path: Path) -> None:
    added = _add(tmp_path)
    out2 = added.parent / "out2.yml"
    result = _run_helper(
        "--file", str(added), "--out", str(out2), "add",
        "--name", "azure-epic1799", "--host", "azure-epic1799",
        "--capabilities", "rust,python",
        "--repos", "claude-coordinator,quadraui",
    )
    assert result.returncode != 0
    assert "already" in result.stderr
