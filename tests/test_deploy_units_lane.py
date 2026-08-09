"""The `deploy/**` lane's deploy step (#1831, wired up by #1835).

#1831 shipped a *detector* — `unit_drift` diffs each host's installed unit
against the units packaged in the wheel — and a remedy a human then typed.
#1835 cannot claim "the fleet reaches that version" while a whole lane needs
a human with `cp` and `systemctl`, so `coord/deploy_units.py` applies what
the detector reports.

Every test here defends one of the three safety properties that make an
*automatic* unit install acceptable at all:

1. **Only units this host already runs get refreshed.** Which services a
   host runs is a topology decision, not a release decision. Installing
   `coord-web.service` onto a machine that never wanted a web server, purely
   because a release contained the file, is worse than a human running `cp`.
2. **Templates are rendered, never copied verbatim (#1928).** A verbatim copy
   installs `<MACHINE_NAME>` as literal text and the unit then refuses to
   start.
3. **The previous content is kept**, so this lane's rollback is a file the
   operator can see and `diff`.

Nothing here needs systemd, a fleet, or root.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coord import deploy_units as du

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def reference(tmp_path: Path) -> Path:
    """A stand-in for `coord/deploy/` inside an installed wheel."""
    ref = tmp_path / "packaged"
    ref.mkdir()
    (ref / "coord-serve.service").write_text("[Service]\nExecStart=new\n")
    (ref / "coord-agent.service").write_text(
        "[Service]\nExecStart=coord agent --machine <MACHINE_NAME> --port <PORT>\n"
    )
    (ref / "coord-web.service").write_text("[Service]\nExecStart=web\n")
    (ref / "coord-serve.timer").write_text("[Timer]\nOnUnitActiveSec=1min\n")
    # Not a unit — must be ignored by the glob, same as unit_drift.
    (ref / "coord-web-dist-build.sh").write_text("#!/bin/sh\n")
    return ref


@pytest.fixture()
def installed(tmp_path: Path) -> Path:
    dest = tmp_path / "systemd-user"
    dest.mkdir()
    (dest / "coord-serve.service").write_text("[Service]\nExecStart=old\n")
    (dest / "coord-agent.service").write_text(
        "[Service]\nExecStart=coord agent --machine dellserver --port 7433\n"
    )
    return dest


def _by_name(report: du.InstallReport) -> dict[str, du.UnitOutcome]:
    return {u.name: u for u in report.units}


# ── property 1: only refresh what this host already runs ─────────────────


def test_a_drifted_unit_is_refreshed(reference, installed):
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="dellserver", port=7433)
    assert report.ok
    outcome = _by_name(report)["coord-serve.service"]
    assert outcome.action == du.ACTION_UPDATED
    assert (installed / "coord-serve.service").read_text() == "[Service]\nExecStart=new\n"


def test_a_packaged_unit_this_host_does_not_run_is_never_installed(reference, installed):
    """A release must not decide which services a host runs."""
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="dellserver", port=7433)
    outcome = _by_name(report)["coord-web.service"]
    assert outcome.action == du.ACTION_NEW
    assert not (installed / "coord-web.service").exists()
    # ...and it is *reported*, not silently dropped, so the human action is
    # visible rather than implicit.
    assert "install and enable it by hand" in outcome.detail


def test_new_units_do_not_make_the_report_unhealthy(reference, installed):
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="dellserver", port=7433)
    assert report.ok


def test_an_already_current_unit_is_untouched(reference, installed):
    (installed / "coord-serve.service").write_text("[Service]\nExecStart=new\n")
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="dellserver", port=7433)
    assert _by_name(report)["coord-serve.service"].action == du.ACTION_UNCHANGED


def test_nothing_changed_means_no_daemon_reload_needed(reference, installed):
    (installed / "coord-serve.service").write_text("[Service]\nExecStart=new\n")
    (installed / "coord-agent.service").write_text(
        "[Service]\nExecStart=coord agent --machine dellserver --port 7433\n"
    )
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="dellserver", port=7433)
    assert not report.changed


def test_non_unit_files_are_ignored(reference, installed):
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="dellserver", port=7433)
    assert "coord-web-dist-build.sh" not in _by_name(report)


# ── property 2: templates are rendered, never copied verbatim ────────────


def test_a_template_is_rendered_for_this_host(reference, installed):
    (installed / "coord-agent.service").write_text("[Service]\nExecStart=stale\n")
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="macmini", port=7433)
    text = (installed / "coord-agent.service").read_text()
    assert "<MACHINE_NAME>" not in text
    assert "--machine macmini" in text
    assert "--port 7433" in text
    assert report.ok


def test_a_template_with_no_value_is_skipped_not_guessed(reference, installed):
    """#1928: copying it verbatim installs `<MACHINE_NAME>` as literal text
    and the unit refuses to start. Refusing loudly beats guessing."""
    (installed / "coord-agent.service").write_text("[Service]\nExecStart=stale\n")
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name=None, port=7433)
    outcome = _by_name(report)["coord-agent.service"]
    assert outcome.action == du.ACTION_SKIPPED
    assert "<MACHINE_NAME>" in outcome.detail
    assert (installed / "coord-agent.service").read_text() == "[Service]\nExecStart=stale\n"
    # A skip is not a failure — the rest of the lane still deployed.
    assert report.ok


def test_render_unit_leaves_placeholderless_text_alone():
    text = "[Service]\nExecStart=x\n"
    rendered, note = du.render_unit(text, machine_name="a", port=1)
    assert rendered == text
    assert note == ""


def test_render_unit_refuses_an_unknown_placeholder():
    rendered, note = du.render_unit("x=<WHO_KNOWS>\n", machine_name="a", port=1)
    assert rendered is None
    assert "WHO_KNOWS" in note


# ── property 3: the previous content is kept ─────────────────────────────


def test_the_previous_unit_is_backed_up(reference, installed):
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="dellserver", port=7433, version="0.4.111")
    outcome = _by_name(report)["coord-serve.service"]
    backup = Path(outcome.backup)
    assert backup.exists()
    assert backup.name == "coord-serve.service.pre-0.4.111.bak"
    assert backup.read_text() == "[Service]\nExecStart=old\n"


# ── dry run ──────────────────────────────────────────────────────────────


def test_a_dry_run_writes_nothing(reference, installed):
    before = (installed / "coord-serve.service").read_text()
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="dellserver", port=7433, dry_run=True)
    assert _by_name(report)["coord-serve.service"].action == du.ACTION_UPDATED
    assert (installed / "coord-serve.service").read_text() == before
    assert not list(installed.glob("*.bak"))


# ── degradation ──────────────────────────────────────────────────────────


def test_a_wheel_with_no_packaged_units_reports_rather_than_crashes(tmp_path):
    """An install predating #1927 ships no `coord/deploy/`. There is nothing
    to deploy from, and saying so is the whole answer."""
    # reference_dir=None falls back to the real packaged dir, which exists in
    # this checkout — so drive the empty case explicitly instead.
    empty = tmp_path / "empty"
    empty.mkdir()
    report = du.install_units(target_dir=tmp_path, reference_dir=empty)
    assert report.units == []
    assert "nothing packaged" in report.summary()


def test_daemon_reload_without_systemd_degrades_to_a_message():
    def _boom(*_args, **_kwargs):
        raise FileNotFoundError("systemctl")

    ok, detail = du.daemon_reload(runner=_boom)
    assert not ok
    assert "no systemd" in detail


def test_daemon_reload_reports_a_nonzero_exit():
    class _Proc:
        returncode = 1
        stderr = "Failed to reload"
        stdout = ""

    ok, detail = du.daemon_reload(runner=lambda *a, **k: _Proc())
    assert not ok
    assert "Failed to reload" in detail


def test_daemon_reload_success():
    class _Proc:
        returncode = 0
        stderr = ""
        stdout = ""

    ok, detail = du.daemon_reload(runner=lambda *a, **k: _Proc())
    assert ok
    assert detail


# ── the real packaged set ────────────────────────────────────────────────


def test_the_real_packaged_units_are_reachable():
    """Guards the guard: if `packaged_unit_dir()` ever stops finding
    `coord/deploy/`, this whole lane silently becomes a no-op that reports
    success — the 2026-08-04 shape."""
    from coord.health.checks.unit_drift import packaged_unit_dir

    assert packaged_unit_dir() is not None


def test_the_propagation_units_ship_in_the_wheel():
    """#1835's own units must ride the lane they created, or the timer that
    propagates every future release can never itself be updated."""
    packaged = REPO_ROOT / "coord" / "deploy"
    assert (packaged / "coord-release-propagate.service").exists()
    assert (packaged / "coord-release-propagate.timer").exists()
