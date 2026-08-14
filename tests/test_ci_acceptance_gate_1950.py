"""#1950: the sealed oracle-loop acceptance suite (tests/acceptance/ms-NN/,
`npm run test:acceptance` / playwright.acceptance.config.ts) was run by no
automatic gate anywhere — `coord acceptance run`/`record` only fires while a
milestone is actively being driven, so a slice that goes red after its
milestone closes (ms-51 at #1547, silently, through #1548/#1550/#1551/#1818)
is never re-checked.

This is the regression guard for the fix: `.github/workflows/test.yml` gets
an `acceptance` job that runs the sealed suite on every push/PR (see that
job's own header comment for why CI rather than
scripts/coord-test-runner.sh). Parsing the real workflow file — not a
fixture — means this test fails the moment someone removes/disables/
neuters that job, which is exactly the silent-regression class #1950
reported (the evidence in that issue was "no workflow in
.github/workflows/ references test:acceptance" — grep, not judgement).

#2180 update: the `acceptance` job's own step no longer runs `npm run
test:acceptance` directly — it runs THROUGH `coord acceptance run --all
--ci` (#2164), which shells out to the same underlying `npm run
test:acceptance` command via `.github/coord-ci-acceptance.yml`'s
web-playwright route. The regression this test guards against is unchanged
(the sealed suite silently stops running), so the needle moves to the
wrapper invocation; a second test (`test_acceptance_job_uses_the_2164_ci_
wrapper_not_the_raw_driver`) pins the specific `--all --ci` contract #2180's
own acceptance criteria require ("through coord acceptance run --all --ci,
not the raw driver command").
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "test.yml"
CARGO_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "cargo-test.yml"
CI_ACCEPTANCE_CONFIG_PATH = REPO_ROOT / ".github" / "coord-ci-acceptance.yml"


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
    test_step = _step_runs(steps, "coord acceptance run")
    assert test_step is not None, (
        "no step in the 'acceptance' job runs `coord acceptance run` — "
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
    test_step = _step_runs(steps, "coord acceptance run")
    assert test_step is not None
    assert test_step.get("continue-on-error") is not True, (
        "the `coord acceptance run` step has continue-on-error: true — "
        "a red sealed suite would no longer fail the job"
    )


def test_acceptance_job_uses_the_2164_ci_wrapper_not_the_raw_driver() -> None:
    """#2180's own acceptance criterion: 'Each repo's CI executes its sealed
    acceptance suite through `coord acceptance run --all --ci`, not the raw
    driver command.' A step that ran `npm run test:acceptance` directly (the
    pre-#2180 shape) would never honour a manifest's `expected_red:`
    registry — a sealed slice authored red by design would fail this job
    exactly like any other red result, forcing `--force-merge` (#2164's
    whole reason for existing)."""
    workflow = _load_workflow()
    steps = _job_steps(workflow, "acceptance")
    test_step = _step_runs(steps, "coord acceptance run")
    assert test_step is not None
    run = test_step["run"]
    assert "--all" in run, f"acceptance step doesn't pass --all: {run!r}"
    assert "--ci" in run, f"acceptance step doesn't pass --ci: {run!r}"
    assert "--repo claude-coordinator" in run, (
        f"acceptance step doesn't scope to this repo: {run!r}"
    )
    # The raw driver command must not appear directly in the WORKFLOW step —
    # it's meant to live only inside .github/coord-ci-acceptance.yml's
    # web-playwright route, invoked BY the wrapper, not instead of it.
    assert "npm run test:acceptance" not in run, (
        "the acceptance step still runs `npm run test:acceptance` directly "
        f"alongside the wrapper — that bypasses expected_red: {run!r}"
    )


def test_ci_acceptance_config_exists_and_resolves_all_three_routes() -> None:
    """.github/coord-ci-acceptance.yml is what every `coord acceptance run
    --all --ci` step in CI points `--config` at (the real
    ~/.coord/coordinator.yml is outside this repo and unreachable from a
    runner). Parse it with the SAME loader coord.cli uses, and confirm all
    three in-repo routes (#1125: coord/dashboard/webapp/** -> web-playwright,
    coord/** -> cli-pytest, tui/** -> tui-tuidriver) resolve — a typo'd
    `match:` glob or a missing route would silently make some CI step's
    `--for-path` resolve to nothing (a loud `sys.exit(1)`, but only ever
    discovered by watching that job actually go red in CI, not by this
    faster/local check)."""
    assert CI_ACCEPTANCE_CONFIG_PATH.exists(), (
        f"{CI_ACCEPTANCE_CONFIG_PATH} is missing — every `coord acceptance "
        "run --all --ci --config .github/coord-ci-acceptance.yml` step in "
        "CI would fail to even load a config"
    )
    from coord.config import load as load_coord_config

    cfg = load_coord_config(str(CI_ACCEPTANCE_CONFIG_PATH))
    webapp = cfg.acceptance.driver_for(
        "claude-coordinator", "coord/dashboard/webapp/src/App.tsx"
    )
    cli = cfg.acceptance.driver_for("claude-coordinator", "coord/cli.py")
    tui = cfg.acceptance.driver_for("claude-coordinator", "tui/src/main.rs")
    assert webapp is not None and webapp.kind == "web-playwright"
    assert cli is not None and cli.kind == "cli-pytest"
    assert tui is not None and tui.kind == "tui-tuidriver"
    # #2164's --all always resolves the `{ms}` template to None (scope="all"
    # has no single milestone to substitute) — render_run_command leaves an
    # unreferenced `{ms}` LITERAL in that case rather than stripping it, so
    # a route written for --issue scoping (like the fleet's real
    # `pytest tests/acceptance/{ms}`) would try to collect a path literally
    # named `tests/acceptance/{ms}` here and crash outright. Every route in
    # this CI-only config must be written to already cover its whole
    # accumulated suite without needing a substitution.
    for driver in (webapp, cli, tui):
        assert "{ms}" not in driver.run, (
            f"{driver.kind} route's run command still references {{ms}}, "
            f"which --all leaves unsubstituted and literal: {driver.run!r}"
        )


def test_non_acceptance_test_job_excludes_the_sealed_suite() -> None:
    """#2180's split: the ordinary `test` job's plain `pytest` must not also
    sweep up tests/acceptance/ms-37's .py files — that would make a
    red-by-design slice (one listed in some ms-NN/manifest.yml's
    expected_red:) fail this job exactly like an ordinary regression, for
    every PR in the repo, defeating the entire point of the #2164 wrapper
    this file's other tests confirm is wired in elsewhere."""
    workflow = _load_workflow()
    steps = _job_steps(workflow, "test")
    pytest_step = _step_runs(steps, "pytest")
    assert pytest_step is not None
    run = pytest_step["run"]
    assert "--ignore=tests/acceptance" in run, (
        f"the 'test' job's pytest step doesn't exclude tests/acceptance: {run!r}"
    )


def test_cargo_test_workflow_runs_the_tui_sealed_suite_through_the_wrapper() -> None:
    """The tui-tuidriver route (ms-33, ms-38) is reachable from NEITHER the
    plain `cargo test` step in cargo-test.yml (the `acceptance` test target
    requires `--features test-support`, so a bare `cargo test` silently
    skips building it) NOR scripts/coord-test-runner.sh's Test-stage `cargo
    test` (same gate). Before #2180 this route ran in no CI job at all."""
    workflow = yaml.safe_load(CARGO_WORKFLOW_PATH.read_text())
    steps = _job_steps(workflow, "cargo-test")
    test_step = _step_runs(steps, "coord acceptance run")
    assert test_step is not None, (
        "no step in cargo-test.yml's 'cargo-test' job runs `coord "
        "acceptance run` — the tui-tuidriver sealed route (ms-33/ms-38) is "
        "unreachable from CI"
    )
    run = test_step["run"]
    assert "--all" in run and "--ci" in run, (
        f"cargo-test.yml's acceptance step doesn't use the #2164 CI "
        f"wrapper contract: {run!r}"
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
