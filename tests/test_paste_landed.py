"""Unit tests for :func:`coord.providers.claude_pty.paste_landed` and
:func:`coord.providers.claude_pty.paste_landed_bytes` (#896).

Root cause of #896: the bare ``fingerprint_in_text`` / ``fingerprint_in_bytes``
checks gave false negatives on large review briefings because Claude Code
collapses big pastes into a placeholder chip
(``❯ [Pasted text #1 +NNN lines]``) and the literal fingerprint (taken from
the *start* of the briefing) never appears in the visible pane.  The
predicates below cover all five required cases without needing a live tmux or
PTY session.
"""

from __future__ import annotations

import pytest

from coord.providers.claude_pty import (
    INPUT_BOX_MARKER,
    INPUT_BOX_MARKER_BYTES,
    briefing_fingerprint,
    paste_landed,
    paste_landed_bytes,
)

# ---------------------------------------------------------------------------
# Fixtures: representative captured-pane strings
# ---------------------------------------------------------------------------

# An empty input-box placeholder — what the pane looks like before any paste.
_EMPTY_BOX = f"{INPUT_BOX_MARKER} Try a task, ask a question, or type /help\n\n"

# A pane where the pasted text is short enough to appear in full.
_FINGERPRINT_VISIBLE = (
    "╭─────────────────────────────────────────╮\n"
    "│ Claude Code                             │\n"
    "╰─────────────────────────────────────────╯\n"
    f"{INPUT_BOX_MARKER} Fix the bug in issue #42: the login\n"
    "flow does not redirect after success\n"
)

# A pane where Claude Code collapsed the paste to a chip (large briefing).
_CHIP_PANE = (
    "╭─────────────────────────────────────────╮\n"
    "│ Claude Code                             │\n"
    "╰─────────────────────────────────────────╯\n"
    f"{INPUT_BOX_MARKER} [Pasted text #1 +47 lines]\n"
)

# A pane where only the TAIL of the briefing is visible (box scrolled to end).
# The fingerprint (first 40 chars) is NOT in this pane, but the content
# differs from the baseline empty placeholder.
_TAIL_ONLY_PANE = (
    "╭─────────────────────────────────────────╮\n"
    "│ Claude Code                             │\n"
    "╰─────────────────────────────────────────╯\n"
    f"{INPUT_BOX_MARKER} ...and make sure the CI stays green.\n"
)

# A pane where an async startup banner changed the top section BUT the input
# box is still the empty placeholder — should NOT count as a paste.
_BANNER_ONLY_PANE = (
    "╭─────────────────────────────────────────╮\n"
    "│ Fable 5 is back — what's new →          │\n"
    "╰─────────────────────────────────────────╯\n"
    f"{INPUT_BOX_MARKER} Try a task, ask a question, or type /help\n\n"
)

# Short fingerprint used in the fingerprint-visible / tail-only tests.
_FP = briefing_fingerprint(
    "Fix the bug in issue #42: the login flow does not redirect after success\n"
    "Background context for the fix follows here.",
)


# ===========================================================================
# paste_landed — text path (tmux capture-pane)
# ===========================================================================


class TestPasteLandedText:
    """Unit tests for :func:`paste_landed`."""

    # -- Fast path: literal fingerprint -------------------------------------

    def test_literal_fingerprint_returns_true(self) -> None:
        """When the fingerprint appears in the pane, paste_landed is True
        regardless of chip or baseline (fast-path, no scoping needed)."""
        assert paste_landed(_FINGERPRINT_VISIBLE, fingerprint=_FP) is True

    def test_literal_fingerprint_anywhere_in_pane_returns_true(self) -> None:
        """Fingerprint match anywhere (not just input-box region) succeeds."""
        pane = f"some header\n{_FP}\n{INPUT_BOX_MARKER} also here"
        assert paste_landed(pane, fingerprint=_FP) is True

    # -- Paste-chip path ----------------------------------------------------

    def test_paste_chip_in_input_box_region_returns_true(self) -> None:
        """[Pasted text #1 +NNN lines] in the input-box region → landed.

        This is the primary #896 regression: a large review briefing gets
        collapsed to a chip and the literal fingerprint is never in the pane.
        """
        fp = briefing_fingerprint("x" * 200)  # first-40 chars will be "xxxx..."
        assert paste_landed(_CHIP_PANE, fingerprint=fp) is True

    def test_paste_chip_variant_plus_lines_suffix(self) -> None:
        """'+N lines]' suffix alone also matches the chip regex."""
        pane = f"{INPUT_BOX_MARKER} [Pasted text #3 +123 lines]\n"
        fp = briefing_fingerprint("a" * 200)
        assert paste_landed(pane, fingerprint=fp) is True

    def test_paste_chip_above_input_box_does_not_match(self) -> None:
        """A chip that appears ABOVE the input-box marker (in the banner area)
        must NOT count — only the input-box region matters."""
        pane = (
            "header [Pasted text #1 +99 lines]\n"
            f"{INPUT_BOX_MARKER} Try a task, ask a question, or type /help\n"
        )
        fp = briefing_fingerprint("something long " * 20)
        # The chip is above the marker; the input-box region is just the
        # empty placeholder.  Without a baseline comparison this should be
        # False because the region equals the baseline placeholder text.
        # (No baseline supplied → only fingerprint + chip checks run.)
        assert paste_landed(pane, fingerprint=fp) is False

    # -- Box-changed / tail-only path --------------------------------------

    def test_tail_visible_differs_from_baseline_returns_true(self) -> None:
        """Only the briefing tail is visible (fingerprint from start absent),
        but the input-box region differs from the baseline → landed."""
        fp = briefing_fingerprint("Fix the bug in issue #42: " + "x" * 100)
        assert (
            paste_landed(_TAIL_ONLY_PANE, fingerprint=fp, baseline=_EMPTY_BOX)
            is True
        )

    def test_box_changed_with_chip_chip_is_detected_first(self) -> None:
        """When a chip is present AND the box changed, chip detection fires
        first (before the baseline comparison)."""
        fp = briefing_fingerprint("y" * 200)
        # Both signals present — result must be True regardless of which
        # branch fires first.
        assert paste_landed(_CHIP_PANE, fingerprint=fp, baseline=_EMPTY_BOX) is True

    # -- Empty box / no paste -----------------------------------------------

    def test_empty_box_no_baseline_returns_false(self) -> None:
        """Pane shows only the empty placeholder, no baseline — not landed."""
        fp = briefing_fingerprint("This briefing was never pasted")
        assert paste_landed(_EMPTY_BOX, fingerprint=fp) is False

    def test_empty_box_same_as_baseline_returns_false(self) -> None:
        """Pane == baseline (paste never happened) → not landed.

        This guards the box-changed branch: if the box looks identical to
        the pre-paste baseline, we must NOT report success.
        """
        fp = briefing_fingerprint("This briefing was never pasted")
        assert paste_landed(_EMPTY_BOX, fingerprint=fp, baseline=_EMPTY_BOX) is False

    # -- Async banner must NOT count as a paste signal ----------------------

    def test_banner_changes_top_but_input_box_unchanged_returns_false(self) -> None:
        """An async startup banner mutating the top of the pane must NOT be
        mistaken for a paste — only the input-box region counts (#865 guard).

        Both the banner pane and the empty-box baseline have the same
        input-box region, so paste_landed must return False.
        """
        fp = briefing_fingerprint("Some important briefing that never landed")
        assert (
            paste_landed(_BANNER_ONLY_PANE, fingerprint=fp, baseline=_EMPTY_BOX)
            is False
        )

    def test_banner_without_baseline_does_not_create_false_positive(self) -> None:
        """Without a baseline, the banner pane (empty input box) still returns
        False — the chip regex doesn't match and no box-changed check runs."""
        fp = briefing_fingerprint("Briefing that never landed")
        assert paste_landed(_BANNER_ONLY_PANE, fingerprint=fp) is False

    # -- No input-box marker in pane ----------------------------------------

    def test_no_marker_in_pane_returns_false(self) -> None:
        """If INPUT_BOX_MARKER is absent (e.g. render incomplete), the chip
        and box-changed checks are skipped and False is returned."""
        pane = "Still loading...\n[Pasted text #1 +10 lines]\n"
        fp = briefing_fingerprint("long briefing " * 20)
        assert paste_landed(pane, fingerprint=fp) is False

    # -- Empty fingerprint --------------------------------------------------

    def test_empty_fingerprint_trivially_true(self) -> None:
        """An empty fingerprint (nothing to verify) always returns True."""
        assert paste_landed("anything", fingerprint="") is True

    def test_whitespace_fingerprint_is_empty_after_normalization(self) -> None:
        """briefing_fingerprint on a whitespace-only string yields ''; trivially True."""
        fp = briefing_fingerprint("   \n\t  ")
        assert fp == ""
        assert paste_landed(_EMPTY_BOX, fingerprint=fp) is True

    # -- No regression on existing fingerprint_in_text behaviour -----------

    def test_fingerprint_whitespace_normalized(self) -> None:
        """Fingerprint still matches when pane re-wraps with different whitespace."""
        pane = (
            f"{INPUT_BOX_MARKER} Fix  the  bug  in  issue  #42:  the  login\n"
            "flow    does    not    redirect\n"
        )
        fp = briefing_fingerprint("Fix the bug in issue #42: the login")
        assert paste_landed(pane, fingerprint=fp) is True


# ===========================================================================
# paste_landed_bytes — bytes path (PTY log)
# ===========================================================================


class TestPasteLandedBytes:
    """Unit tests for :func:`paste_landed_bytes`."""

    def _box_bytes(self, content: str = "") -> bytes:
        """Build raw bytes containing INPUT_BOX_MARKER_BYTES and optional content."""
        box = f"{INPUT_BOX_MARKER} {content}\n" if content else f"{INPUT_BOX_MARKER} placeholder\n"
        return b"READY_BANNER\r\n" + box.encode("utf-8")

    # -- Fast path: literal fingerprint ------------------------------------

    def test_literal_fingerprint_found(self) -> None:
        """Fingerprint present in bytes → True (fast path)."""
        briefing = "Fix the bug in issue #42"
        fp = briefing_fingerprint(briefing)
        raw = self._box_bytes(briefing)
        assert paste_landed_bytes(raw, fp) is True

    def test_literal_fingerprint_with_whitespace_normalization(self) -> None:
        """fingerprint_in_bytes collapses whitespace — re-wrapped text still matches."""
        fp = briefing_fingerprint("Fix the bug")
        raw = b"output\r\n" + INPUT_BOX_MARKER_BYTES + b" Fix  the  bug\r\n"
        assert paste_landed_bytes(raw, fp) is True

    # -- Paste-chip path ---------------------------------------------------

    def test_paste_chip_after_marker_returns_true(self) -> None:
        """[Pasted text … +NNN lines] after INPUT_BOX_MARKER_BYTES → True."""
        fp = briefing_fingerprint("z" * 200)
        raw = (
            b"READY_BANNER\r\n"
            + INPUT_BOX_MARKER_BYTES
            + b" [Pasted text #1 +47 lines]\r\n"
        )
        assert paste_landed_bytes(raw, fp) is True

    def test_paste_chip_before_marker_does_not_match(self) -> None:
        """A chip that appears BEFORE the input-box marker is not in scope."""
        fp = briefing_fingerprint("b" * 200)
        raw = (
            b"[Pasted text #1 +47 lines]\r\n"
            b"READY_BANNER\r\n"
            + INPUT_BOX_MARKER_BYTES
            + b" placeholder\r\n"
        )
        # Chip is before the marker; region after marker is just the
        # placeholder — no chip match there.
        assert paste_landed_bytes(raw, fp) is False

    def test_paste_chip_when_no_marker_returns_false(self) -> None:
        """Chip in bytes but no INPUT_BOX_MARKER_BYTES → False (can't scope)."""
        fp = briefing_fingerprint("c" * 200)
        raw = b"[Pasted text #1 +10 lines]\r\n"
        assert paste_landed_bytes(raw, fp) is False

    # -- Empty fingerprint -------------------------------------------------

    def test_empty_fingerprint_trivially_true(self) -> None:
        assert paste_landed_bytes(b"anything", "") is True

    # -- No regression: empty bytes / no paste ----------------------------

    def test_empty_bytes_with_fingerprint_returns_false(self) -> None:
        fp = briefing_fingerprint("Something that never landed")
        assert paste_landed_bytes(b"", fp) is False

    def test_ready_banner_only_no_paste_returns_false(self) -> None:
        """A banner with the empty placeholder only → not landed."""
        fp = briefing_fingerprint("Something that never landed " * 10)
        raw = b"READY_BANNER\r\n" + INPUT_BOX_MARKER_BYTES + b" placeholder\r\n"
        assert paste_landed_bytes(raw, fp) is False
