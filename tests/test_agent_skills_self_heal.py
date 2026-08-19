"""Skills self-heal: `coord install-skills` (#319) was a real fix for a real
problem — a bundled `coord/skills/*/SKILL.md` file only reaches a worker
machine's `~/.claude/skills/` if someone runs it there manually. Nothing in
agent provisioning or the health tick ever did, so a skill added or updated
in a coordinator release could sit uninstalled indefinitely, unnoticed — the
same "silent gap" shape as the graphify hooks and the browser-capability
probe.

This suite exercises `AgentServer._self_heal_missing_skills`, wired into the
same cached `/health` tick as the graph self-heal
(`test_agent_graph_self_heal.py`). Unlike that pass, this one needs none of
its four guards — syncing a handful of small text files is cheap file I/O,
not a CPU-heavy subprocess — so there is no idle-gate, no in-flight dedup,
and no retry budget to test. What matters here: it actually installs, it is
idempotent on a repeat tick, it surfaces on `/health`, and a failure to
locate the bundled package never blinds `/health` itself.
"""

from __future__ import annotations

from pathlib import Path

from coord.agent import AgentServer


def _server(tmp_path: Path, **kwargs) -> AgentServer:
    return AgentServer(
        machine_name="test",
        state_dir=tmp_path / "state",
        **kwargs,
    )


def test_missing_skills_get_installed(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    server = _server(tmp_path)
    server._self_heal_missing_skills()

    installed = list((fake_home / ".claude" / "skills").glob("*/SKILL.md"))
    assert installed, "expected at least one bundled skill to be installed"
    assert any(p.parent.name == "update-issue" for p in installed)

    assert server._skills_heal_passes == 1
    assert server._skills_heal_last_run_at is not None
    assert server._skills_heal_last_error is None
    synced_names = {c["skill"] for c in server._skills_heal_last_synced}
    assert "update-issue" in synced_names
    assert all(c["action"] == "installed" for c in server._skills_heal_last_synced)


def test_repeat_tick_is_a_quiet_no_op(tmp_path: Path, monkeypatch) -> None:
    """Once every bundled skill is current, a repeat tick changes nothing —
    load-bearing so this can ride a TTL tick forever without churn."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    server = _server(tmp_path)
    server._self_heal_missing_skills()
    first_pass_mtime = (fake_home / ".claude" / "skills" / "update-issue" / "SKILL.md").stat().st_mtime

    server._self_heal_missing_skills()

    assert server._skills_heal_passes == 2
    assert server._skills_heal_last_synced == []
    second_pass_mtime = (fake_home / ".claude" / "skills" / "update-issue" / "SKILL.md").stat().st_mtime
    assert first_pass_mtime == second_pass_mtime, "unchanged content must not be rewritten"


def test_stale_skill_gets_updated_not_skipped(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    dest = fake_home / ".claude" / "skills" / "update-issue"
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text("# stale content from a previous release\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    server = _server(tmp_path)
    server._self_heal_missing_skills()

    current = (dest / "SKILL.md").read_text(encoding="utf-8")
    assert current != "# stale content from a previous release\n"
    synced = {c["skill"]: c["action"] for c in server._skills_heal_last_synced}
    assert synced["update-issue"] == "updated"


def test_health_surfaces_the_skills_self_heal_block(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    server = _server(tmp_path)
    payload = server.health()

    block = payload["skills_self_heal"]
    assert block["passes"] == 1
    assert block["last_run_at"] is not None
    assert block["last_error"] is None
    assert any(c["skill"] == "update-issue" for c in block["last_synced"])
    assert (fake_home / ".claude" / "skills" / "update-issue" / "SKILL.md").exists()


def test_package_lookup_failure_is_recorded_not_raised(tmp_path: Path, monkeypatch) -> None:
    """A broken bundled-skills package must be visible on `/health`, not a
    crashed poll — same fail-soft contract as the graph self-heal pass."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    def _boom() -> list:
        raise ModuleNotFoundError("coord.skills")

    monkeypatch.setattr("coord.commands.setup.list_bundled_skill_dirs", _boom)

    server = _server(tmp_path)
    payload = server.health()  # must not raise

    block = payload["skills_self_heal"]
    assert block["passes"] == 0
    assert "ModuleNotFoundError" in block["last_error"]
    assert not (fake_home / ".claude" / "skills").exists()
