"""#1242 (PKG-6): one `v*` tag -> one GitHub Release, everything at one version.

Two layers, matching the two ways this can regress:

* ``TestUnifiedReleaseWorkflow`` parses the real
  ``.github/workflows/{publish,release-tui}.yml`` — not a fixture — and pins
  the *structural* invariant the unification exists for.  Before it, both
  workflows fired on `v*` and both called ``softprops/action-gh-release`` for
  the same tag; that action upserts by ``tag_name``, so whichever run's step
  landed first created the Release and the other appended to it.  Which meant
  whether the Release carried generated notes was decided by a race between
  two independent workflow runs — and neither attached the wheel at all.  The
  fix has exactly one release-creating step in the whole system, so that's
  what these assert: one `v*` trigger, one ``action-gh-release`` step,
  release-tui.yml reachable only as a reusable workflow.

  This is a grep-shaped test on purpose (cf. tests/test_ci_acceptance_gate_1950.py):
  you cannot run GitHub Actions in pytest, and the failure mode being guarded
  is someone re-adding a second release-creating step, which reads as
  perfectly sensible YAML in isolation.

* ``TestVerifyReleaseWheel`` drives ``scripts/verify_release_wheel.py``
  against synthetic dists.  That script is the CI-observable half of "all
  stamped the same version": it fails the publish job when setuptools-scm
  resolved a ``.devN+g<sha>`` fallback instead of the tag (the shallow-clone
  failure mode #1238's ``fallback_version`` deliberately makes non-fatal for
  dev checkouts), when the ``[server]`` extra is missing from the built
  metadata (PKG-1/#1237 — every agent host installs it), or when the wheel
  was built without the React bundle.
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
PUBLISH_YML = WORKFLOW_DIR / "publish.yml"
RELEASE_TUI_YML = WORKFLOW_DIR / "release-tui.yml"
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify_release_wheel.py"

sys.path.insert(0, str(REPO_ROOT))

from scripts.verify_release_wheel import (  # noqa: E402  - needs the sys.path line above
    VerificationError,
    verify,
)


def _load(path: Path) -> dict:
    # YAML 1.1 (SafeLoader) parses a bare `on:` key as the boolean True, so
    # triggers live under `True`, not `"on"`. `_triggers` normalises that.
    return yaml.safe_load(path.read_text())


def _triggers(workflow: dict) -> dict:
    on = workflow.get("on", workflow.get(True))
    assert isinstance(on, dict), f"expected a mapping of triggers, got {on!r}"
    return on


def _all_steps(workflow: dict) -> list[tuple[str, dict]]:
    """Every ``(job_name, step)`` pair in *workflow*."""
    out: list[tuple[str, dict]] = []
    for job_name, job in (workflow.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            out.append((job_name, step))
    return out


def _steps_using(workflow: dict, action_prefix: str) -> list[tuple[str, dict]]:
    return [
        (job, step)
        for job, step in _all_steps(workflow)
        if isinstance(step.get("uses"), str) and step["uses"].startswith(action_prefix)
    ]


def _transitive_needs(workflow: dict, job_name: str) -> set[str]:
    """Every job *job_name* waits on, directly or through another job."""
    jobs = workflow["jobs"]
    seen: set[str] = set()
    stack = list(jobs[job_name].get("needs") or [])
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        stack.extend(jobs.get(name, {}).get("needs") or [])
    return seen


# ──────────────────────────────────────────────────────────────────────────
# structural: one tag, one release
# ──────────────────────────────────────────────────────────────────────────


class TestUnifiedReleaseWorkflow:
    def test_publish_is_the_only_v_tag_entrypoint(self) -> None:
        publish_tags = _triggers(_load(PUBLISH_YML))["push"]["tags"]
        assert publish_tags == ["v*"], publish_tags

        tui_triggers = _triggers(_load(RELEASE_TUI_YML))
        assert "push" not in tui_triggers, (
            "release-tui.yml still fires on a push trigger of its own — that is "
            "the two-workflows-race-for-one-Release bug #1242 fixed. It must be "
            "reachable only via publish.yml's workflow_call (plus its "
            "workflow_dispatch dry run)."
        )
        assert "workflow_call" in tui_triggers, (
            "release-tui.yml must be a reusable workflow so publish.yml can run "
            "its build matrix inside the same run as the wheel build"
        )

    def test_publish_calls_the_tui_workflow_with_the_tag(self) -> None:
        jobs = _load(PUBLISH_YML)["jobs"]
        callers = [
            (name, job)
            for name, job in jobs.items()
            if isinstance(job.get("uses"), str) and "release-tui.yml" in job["uses"]
        ]
        assert len(callers) == 1, (
            "expected exactly one job in publish.yml calling release-tui.yml as a "
            f"reusable workflow; found {[n for n, _ in callers]}"
        )
        _, job = callers[0]
        # "Everything at one version" means the tag is handed down from one
        # place, not independently re-derived on both sides.
        assert "tag" in (job.get("with") or {}), (
            "publish.yml must pass the tag down to release-tui.yml so the "
            "binaries and the wheel are stamped from the same value"
        )
        declared = _triggers(_load(RELEASE_TUI_YML))["workflow_call"]["inputs"]
        assert "tag" in declared, f"release-tui.yml declares inputs {sorted(declared)}"

    def test_exactly_one_step_creates_the_github_release(self) -> None:
        release_steps = [
            (PUBLISH_YML.name, job, step)
            for job, step in _steps_using(_load(PUBLISH_YML), "softprops/action-gh-release")
        ] + [
            (RELEASE_TUI_YML.name, job, step)
            for job, step in _steps_using(_load(RELEASE_TUI_YML), "softprops/action-gh-release")
        ]
        assert len(release_steps) == 1, (
            "more than one step creates/updates the GitHub Release: "
            f"{[(f, j) for f, j, _ in release_steps]}. action-gh-release upserts "
            "by tag_name, so two of them racing for one tag is precisely the "
            "#1242 bug — whichever landed first decided whether the Release got "
            "generated notes."
        )
        _, _, step = release_steps[0]
        assert step["with"].get("generate_release_notes") is True

    def test_the_release_carries_the_wheel_and_the_binaries(self) -> None:
        workflow = _load(PUBLISH_YML)
        release_job_name = next(
            name
            for name, job in workflow["jobs"].items()
            if _steps_using({"jobs": {name: job}}, "softprops/action-gh-release")
        )
        # It can only publish what it waited for.
        waits_on = _transitive_needs(workflow, release_job_name)
        assert {"build-wheel", "build-tui", "verify-assets"} <= waits_on, (
            f"the release job waits on {sorted(waits_on)} — it must gate on the "
            "wheel build, the coord-tui build, and the completeness check, or it "
            "can publish a Release that is missing half the system"
        )

        check = "\n".join(
            step.get("run", "")
            for step in workflow["jobs"]["verify-assets"]["steps"]
            if step.get("run")
        )
        assert "*.whl" in check, "verify-assets never collects the wheel as an asset"
        for target in ("x86_64-linux", "x86_64-macos", "aarch64-macos"):
            assert target in check, (
                f"verify-assets does not assert coord-tui-{target} is present; "
                "a silently binary-less release is what PKG-3's acceptance bar "
                "forbids"
            )

    def test_dry_run_builds_and_checks_everything_but_publishes_nothing(self) -> None:
        """The acceptance criterion's "(or dry-run)": a maintainer must be able
        to prove the whole asset set builds and agrees on a version *without*
        an irreversible PyPI upload — this workflow's own changes cannot
        otherwise be tested before they run for real."""
        workflow = _load(PUBLISH_YML)
        jobs = workflow["jobs"]

        assert "dry_run_tag" in _triggers(workflow)["workflow_dispatch"]["inputs"]

        for name in ("verify-tag", "publish-pypi", "release"):
            condition = jobs[name].get("if", "")
            assert "dry_run" in str(condition), (
                f"job {name!r} has no dry-run guard (if: {condition!r}) — a dry "
                "run would publish to PyPI / cut a real Release"
            )

        # ...while the jobs that prove the release is complete carry no such
        # guard, so a dry run exercises them in full.
        for name in ("build-wheel", "build-tui", "verify-assets"):
            assert "dry_run" not in str(jobs[name].get("if", "")), (
                f"job {name!r} is skipped on a dry run, which defeats the point "
                "of having one"
            )

    def test_only_the_wheel_build_and_the_dry_run_touch_versions(self) -> None:
        """A dry run is dispatched against a branch, so setuptools-scm has no
        release tag to find and would silently build `X.Y.Z.devN+g<sha>`."""
        steps = _load(PUBLISH_YML)["jobs"]["build-wheel"]["steps"]
        tagging = [s for s in steps if "git tag" in s.get("run", "")]
        assert len(tagging) == 1, "expected exactly one throwaway-tag step"
        assert "dry_run" in str(tagging[0].get("if", "")), (
            "the throwaway local tag must be dry-run-only — creating one during "
            "a real release would mask the tag being published"
        )

    def test_wheel_build_checks_out_tags_for_setuptools_scm(self) -> None:
        jobs = _load(PUBLISH_YML)["jobs"]
        wheel_job = jobs["build-wheel"]
        checkout = next(
            step
            for step in wheel_job["steps"]
            if isinstance(step.get("uses"), str) and step["uses"].startswith("actions/checkout")
        )
        # setuptools-scm falls back to `X.Y.Z.devN+g<sha>` rather than failing
        # when no tag is reachable (#1238) — a shallow clone would therefore
        # publish a dev version under a `vX.Y.Z` release, immutably.
        assert checkout["with"]["fetch-depth"] == 0
        assert checkout["with"].get("fetch-tags") is True

    def test_wheel_build_runs_the_artifact_verifier(self) -> None:
        steps = _load(PUBLISH_YML)["jobs"]["build-wheel"]["steps"]
        runs = "\n".join(step.get("run", "") for step in steps if step.get("run"))
        assert "scripts/verify_release_wheel.py" in runs, (
            "publish.yml no longer verifies the built wheel against the tag — "
            "that check is the only thing standing between a shallow clone and "
            "an immutable PyPI upload of the wrong version"
        )
        # #2009: the wheel no longer carries a webapp bundle — the source
        # moved to the `coord-web` repo, so there is nothing here to `npm run
        # build`. `--no-webapp` is what keeps the REST of the verifier
        # (version stamp, `[server]` extra) blocking the release: dropping
        # the flag would fail every release on the now-permanently-absent
        # bundle, and dropping the step would stop checking anything.
        assert "npm run build" not in runs, (
            "publish.yml is trying to build a React bundle from source this "
            "repo no longer has (#2009) — the webapp lives in coord-web"
        )
        assert "--no-webapp" in runs, (
            "publish.yml must pass --no-webapp now that the wheel carries no "
            "bundle, or every release fails on a deliberately-absent artifact"
        )

    def test_publish_workflow_installs_no_node_toolchain(self) -> None:
        """#2009: nothing in the release path builds JavaScript any more.

        A leftover `actions/setup-node` would be the harmless-looking half of
        a re-added webapp build step, and the expensive half (`npm ci`
        against a missing directory) fails loudly enough to not need a test.
        """
        steps = _load(PUBLISH_YML)["jobs"]["build-wheel"]["steps"]
        uses = [step.get("uses", "") for step in steps]
        assert not [u for u in uses if u.startswith("actions/setup-node")], (
            f"build-wheel still sets up Node with nothing to build: {uses}"
        )

    def test_tui_workflow_uploads_the_asset_names_coord_tui_update_expects(self) -> None:
        from coord.tui_release import asset_filename

        steps = _load(RELEASE_TUI_YML)["jobs"]["build"]["steps"]
        upload = next(
            step
            for step in steps
            if isinstance(step.get("uses"), str)
            and step["uses"].startswith("actions/upload-artifact")
        )
        path = upload["with"]["path"]
        # `${{ matrix.target_name }}`/`${{ matrix.bin_ext }}` substituted by hand.
        for target in ("x86_64-linux", "aarch64-macos", "x86_64-windows"):
            ext = ".exe" if target.endswith("-windows") else ""
            rendered = (
                path.replace("${{ matrix.target_name }}", target).replace(
                    "${{ matrix.bin_ext }}", ext
                )
            )
            assert rendered.endswith(asset_filename(target)), (
                f"release-tui.yml uploads {rendered!r} but coord/tui_release.py's "
                f"`coord tui update` looks for {asset_filename(target)!r}"
            )


# ──────────────────────────────────────────────────────────────────────────
# scripts/verify_release_wheel.py
# ──────────────────────────────────────────────────────────────────────────


def _make_wheel(
    dist: Path,
    version: str,
    *,
    extra: str | None = "server",
    webapp: bool = True,
    name: str = "code_coordinator",
) -> Path:
    path = dist / f"{name}-{version}-py3-none-any.whl"
    metadata = f"Metadata-Version: 2.1\nName: code-coordinator\nVersion: {version}\n"
    if extra:
        metadata += f"Provides-Extra: {extra}\n"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{name}-{version}.dist-info/METADATA", metadata)
        zf.writestr("coord/__init__.py", "")
        if webapp:
            zf.writestr("coord/dashboard/webapp/dist/index.html", "<html></html>")
    return path


def _make_sdist(dist: Path, version: str, name: str = "code_coordinator") -> Path:
    path = dist / f"{name}-{version}.tar.gz"
    path.write_bytes(b"")
    return path


@pytest.fixture()
def dist(tmp_path: Path) -> Path:
    d = tmp_path / "dist"
    d.mkdir()
    return d


class TestVerifyReleaseWheel:
    def test_happy_path_returns_the_tag_version(self, dist: Path) -> None:
        _make_wheel(dist, "0.4.106")
        _make_sdist(dist, "0.4.106")
        assert str(verify(dist, "v0.4.106")) == "0.4.106"

    def test_pep440_normalisation_is_not_a_mismatch(self, dist: Path) -> None:
        # setuptools-scm writes `1.0.0rc1` into the filename for a tag spelled
        # `v1.0.0-rc1`; those are the same version, not a drift.
        _make_wheel(dist, "1.0.0rc1")
        _make_sdist(dist, "1.0.0rc1")
        assert str(verify(dist, "v1.0.0-rc1")) == "1.0.0rc1"

    def test_setuptools_scm_dev_fallback_is_rejected(self, dist: Path) -> None:
        """The shallow-clone / missing-tag failure this exists for: the build
        succeeds, the wheel is valid, and the version is silently wrong."""
        _make_wheel(dist, "0.4.106.dev3+gdeadbee")
        _make_sdist(dist, "0.4.106.dev3+gdeadbee")
        with pytest.raises(VerificationError, match="setuptools-scm did not resolve the"):
            verify(dist, "v0.4.106")

    def test_missing_server_extra_is_rejected(self, dist: Path) -> None:
        _make_wheel(dist, "0.4.106", extra=None)
        _make_sdist(dist, "0.4.106")
        with pytest.raises(VerificationError, match="Provides-Extra: server"):
            verify(dist, "v0.4.106")

    def test_missing_webapp_bundle_is_rejected(self, dist: Path) -> None:
        _make_wheel(dist, "0.4.106", webapp=False)
        _make_sdist(dist, "0.4.106")
        with pytest.raises(VerificationError, match="React bundle was not built"):
            verify(dist, "v0.4.106")

    def test_missing_webapp_bundle_can_be_waived_for_local_builds(self, dist: Path) -> None:
        _make_wheel(dist, "0.4.106", webapp=False)
        _make_sdist(dist, "0.4.106")
        assert str(verify(dist, "v0.4.106", require_webapp=False)) == "0.4.106"

    def test_missing_sdist_is_rejected(self, dist: Path) -> None:
        _make_wheel(dist, "0.4.106")
        with pytest.raises(VerificationError, match="exactly 1 sdist"):
            verify(dist, "v0.4.106")

    def test_stale_wheel_left_in_dist_is_rejected(self, dist: Path) -> None:
        """A rebuilt-without-cleaning `dist/` would otherwise upload two
        versions of the same package under one tag."""
        _make_wheel(dist, "0.4.105")
        _make_wheel(dist, "0.4.106")
        _make_sdist(dist, "0.4.106")
        with pytest.raises(VerificationError, match="exactly 1 wheel"):
            verify(dist, "v0.4.106")

    def test_all_problems_are_reported_together(self, dist: Path) -> None:
        _make_wheel(dist, "0.4.105", extra=None, webapp=False)
        _make_sdist(dist, "0.4.105")
        with pytest.raises(VerificationError) as exc:
            verify(dist, "v0.4.106")
        message = str(exc.value)
        assert "setuptools-scm did not resolve" in message
        assert "Provides-Extra: server" in message
        assert "React bundle was not built" in message

    def test_cli_exits_nonzero_and_annotates_on_failure(self, dist: Path) -> None:
        _make_wheel(dist, "0.4.105")
        _make_sdist(dist, "0.4.105")
        proc = subprocess.run(
            [sys.executable, str(VERIFY_SCRIPT), "--tag", "v0.4.106", "--dist", str(dist)],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 1
        assert "::error::" in proc.stderr

    def test_cli_exits_zero_on_a_good_dist(self, dist: Path) -> None:
        _make_wheel(dist, "0.4.106")
        _make_sdist(dist, "0.4.106")
        proc = subprocess.run(
            [sys.executable, str(VERIFY_SCRIPT), "--tag", "v0.4.106", "--dist", str(dist)],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert "0.4.106" in proc.stdout
