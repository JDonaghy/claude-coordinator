"""Tests for error handling, retry, rate limiting, and concurrency config."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

import httpx
import pytest

from coord.config import ConcurrencyConfig, ConfigError, load
from coord.dispatch import dispatch_with_retry
from coord.models import Machine, Proposal, Repo
from coord.network import classify_error, is_retryable, RATE_LIMITED, TIMEOUT, HTTP_ERROR, OFFLINE, DNS_ERROR


# ── Config parsing ──────────────────────────────────────────────────────────


class TestConcurrencyConfig:
    def test_parsed_from_yaml(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
            "concurrency:\n"
            "  max_workers: 3\n"
            "  stagger_seconds: 15\n"
            "  backoff_base: 30\n"
            "  max_retries: 5\n"
        )
        cfg = load(p)
        assert cfg.concurrency.max_workers == 3
        assert cfg.concurrency.stagger_seconds == 15
        assert cfg.concurrency.backoff_base == 30
        assert cfg.concurrency.max_retries == 5

    def test_defaults_when_missing(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
        )
        cfg = load(p)
        assert cfg.concurrency.max_workers == 2
        assert cfg.concurrency.stagger_seconds == 30.0
        assert cfg.concurrency.backoff_base == 60.0
        assert cfg.concurrency.max_retries == 3

    def test_negative_value_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
            "concurrency:\n  max_workers: -1\n"
        )
        with pytest.raises(ConfigError, match="non-negative"):
            load(p)

    def test_example_config_parses(self) -> None:
        cfg = load(Path(__file__).resolve().parents[1] / "coordinator.yml")
        assert cfg.concurrency.max_workers >= 1


# ── Error classification ────────────────────────────────────────────────────


class TestClassifyError:
    def test_rate_limit_429(self) -> None:
        resp = MagicMock()
        resp.status_code = 429
        exc = httpx.HTTPStatusError("rate limited", request=MagicMock(), response=resp)
        state, reason = classify_error(exc)
        assert state == RATE_LIMITED
        assert "429" in reason

    def test_timeout(self) -> None:
        state, _ = classify_error(httpx.ConnectTimeout("timeout"))
        assert state == TIMEOUT

    def test_connection_refused(self) -> None:
        state, _ = classify_error(httpx.ConnectError("connection refused"))
        assert state == OFFLINE

    def test_dns_error(self) -> None:
        state, _ = classify_error(httpx.ConnectError("name or service not known"))
        assert state == DNS_ERROR


class TestIsRetryable:
    def test_retryable_states(self) -> None:
        assert is_retryable(TIMEOUT)
        assert is_retryable(RATE_LIMITED)
        assert is_retryable(HTTP_ERROR)

    def test_non_retryable_states(self) -> None:
        assert not is_retryable(OFFLINE)
        assert not is_retryable(DNS_ERROR)


# ── Retry with backoff ──────────────────────────────────────────────────────


class TestDispatchWithRetry:
    @pytest.fixture
    def config(self):
        from coord.config import Config
        return Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/tmp/api"},
            )],
        )

    @pytest.fixture
    def proposal(self) -> Proposal:
        return Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=42, issue_title="Fix auth", rationale="test",
        )

    @patch("coord.dispatch.httpx.post")
    @patch("coord.dispatch.time.sleep")
    def test_succeeds_on_first_try(self, mock_sleep, mock_post, config, proposal) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "abc"}
        mock_resp.raise_for_status = lambda: None
        mock_post.return_value = mock_resp

        result = dispatch_with_retry(proposal, config, max_retries=3, backoff_base=1.0)
        assert result["id"] == "abc"
        mock_sleep.assert_not_called()

    @patch("coord.dispatch.httpx.post")
    @patch("coord.dispatch.time.sleep")
    def test_retries_on_timeout(self, mock_sleep, mock_post, config, proposal) -> None:
        timeout_exc = httpx.ConnectTimeout("timed out")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "abc"}
        mock_resp.raise_for_status = lambda: None
        mock_post.side_effect = [timeout_exc, timeout_exc, mock_resp]

        result = dispatch_with_retry(proposal, config, max_retries=3, backoff_base=0.01)
        assert result["id"] == "abc"
        assert mock_sleep.call_count == 2

    @patch("coord.dispatch.httpx.post")
    @patch("coord.dispatch.time.sleep")
    def test_gives_up_after_max_retries(self, mock_sleep, mock_post, config, proposal) -> None:
        mock_post.side_effect = httpx.ConnectTimeout("timed out")

        with pytest.raises(httpx.ConnectTimeout):
            dispatch_with_retry(proposal, config, max_retries=2, backoff_base=0.01)
        assert mock_post.call_count == 3  # initial + 2 retries

    @patch("coord.dispatch.httpx.post")
    def test_no_retry_on_value_error(self, mock_post, config) -> None:
        bad_proposal = Proposal(
            id=1, machine_name="ghost", repo_name="api",
            issue_number=1, issue_title="x", rationale="",
        )
        with pytest.raises(ValueError, match="Unknown machine"):
            dispatch_with_retry(bad_proposal, config)
        mock_post.assert_not_called()

    @patch("coord.dispatch.httpx.post")
    @patch("coord.dispatch.time.sleep")
    def test_on_retry_callback(self, mock_sleep, mock_post, config, proposal) -> None:
        mock_post.side_effect = [
            httpx.ConnectTimeout("timed out"),
            MagicMock(json=lambda: {"id": "x"}, raise_for_status=lambda: None),
        ]
        retries = []
        dispatch_with_retry(
            proposal, config, max_retries=3, backoff_base=0.01,
            on_retry=lambda a, m, s, r, w: retries.append((a, s)),
        )
        assert len(retries) == 1
        assert retries[0] == (1, TIMEOUT)

    @patch("coord.dispatch.httpx.post")
    def test_no_retry_on_offline(self, mock_post, config, proposal) -> None:
        mock_post.side_effect = httpx.ConnectError("connection refused")
        with pytest.raises(httpx.ConnectError):
            dispatch_with_retry(proposal, config, max_retries=3, backoff_base=0.01)
        assert mock_post.call_count == 1
