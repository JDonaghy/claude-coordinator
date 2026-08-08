"""#1950: the sealed oracle-loop acceptance suite (tests/acceptance/ms-NN/,
`npm run test:acceptance` / playwright.acceptance.config.ts) was run by no
automatic gate anywhere — `coord acceptance run`/`record` only fires while a
milestone is actively being driven, so a slice that goes red after its
milestone closes (ms-51 at #1547, silently, through #1548/#1550/#1551/#1818)
is never re-checked.

This is the regression guard for the fix: `.github/workflows/test.yml` gets
an `acceptance` job that runs `npm run test:acceptance` on every push/PR
(see that job's own header comment for why CI rather than
scripts/coord-test-runner.sh). Parsing the real workflow file — not a
fixture — means this test fails the moment someone removes/disables/
neuters that job, which is exactly the silent-regression class #1950
reported (the evidence in that issue was "no workflow in
.github/workflows/ references test:acceptance" — grep, not judgement).
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "test.yml"


def _load_workflow() -> dict:
    # YAML's `on:` key parses to the boolean `True` under the default
    # SafeLoader (YAML 1.1 treats the bare word `on` as a boolean) — harmless
    # here since this test never reads that key, but noted so a future
    # reader isn't surprised the loaded dict's key isn't the string "on".
    return yaml.safe_load(WORKFLOW_PATH.read_text())


def _job_steps(workflow: dict, job_name: str) -> list[dict]:
    jobs = workflow.get("jobs") or {}
    job = jobs.get(job_name)
    assert job is not None, (
        f"expected a {job_name!r} job in {WORKFLOW_PATH} — jobs present: "
        f"{sorted(jobs)}"
    )
    return job.get("steps") or []


def _step_runs(steps: list[dict], needle: str) -> dict | None:
    for step in steps:
        run = step.get("run")
        if isinstance(run, str) and needle in run:
            return step
    return None


def test_acceptance_job_exists_and_runs_the_sealed_suite() -> None:
    workflow = _load_workflow()
    steps = _job_steps(workflow, "acceptance")
    test_step = _step_runs(steps, "npm run test:acceptance")
    assert test_step is not None, (
        "no step in the 'acceptance' job runs `npm run test:acceptance` — "
        "this is the exact bug #1950 reported (the sealed suite run by no "
        "automatic gate anywhere)"
    )


def test_acceptance_job_does_not_neuter_a_red_result() -> None:
    """A gate that swallows its own failure isn't a gate (#1950 item 3's
    "a gate that has never failed is not a gate", #1544 §5). Guards against
    the job being kept in name only via `continue-on-error: true` on the
    step (or the whole job) — the same "green screenshot" failure mode
    #1950 describes, just relocated from "never runs" to "runs but can't
    fail"."""
    workflow = _load_workflow()
    jobs = workflow.get("jobs") or {}
    job = jobs.get("acceptance")
    assert job is not None
    assert job.get("continue-on-error") is not True, (
        "the 'acceptance' job has continue-on-error: true at the job level "
        "— a red sealed suite would no longer fail CI"
    )
    steps = job.get("steps") or []
    test_step = _step_runs(steps, "npm run test:acceptance")
    assert test_step is not None
    assert test_step.get("continue-on-error") is not True, (
        "the `npm run test:acceptance` step has continue-on-error: true — "
        "a red sealed suite would no longer fail the job"
    )


def test_acceptance_job_installs_coord_cli_on_path() -> None:
    """playwright.acceptance.config.ts's `webServer` shells out to `coord
    web --fixture ... --dist dist` (#1818) — without `coord` resolvable on
    PATH the webServer never boots and every spec fails with a webServer
    timeout instead of a real (or real-absence-of) assertion failure. Same
    requirement `e2e` already has for `live-update-fixture.spec.ts`
    (#1551)."""
    workflow = _load_workflow()
    steps = _job_steps(workflow, "acceptance")
    install_step = _step_runs(steps, "pip install")
    assert install_step is not None, (
        "the 'acceptance' job never installs the `coord` CLI (`pip install "
        "-e \".[dev]\"`) — playwright.acceptance.config.ts's webServer "
        "needs `coord` on PATH to boot the fixture-backed dashboard"
    )


def test_acceptance_job_installs_a_real_browser() -> None:
    workflow = _load_workflow()
    steps = _job_steps(workflow, "acceptance")
    browser_step = _step_runs(steps, "playwright install")
    assert browser_step is not None, (
        "the 'acceptance' job never runs `npx playwright install` — "
        "chromium (or whichever project(s) playwright.acceptance.config.ts "
        "declares) won't be present to actually run the suite"
    )
