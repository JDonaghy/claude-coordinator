"""#2507: milestone <-> portal submission_id linkage.

Covers the domain layer (`coord.portal_store.PortalLink` + its wrapper
functions) and the underlying board_meta persistence in `coord.state`, the
same split `tests/test_gate_a.py`'s `TestPersistence` covers for
`GateAApproval` / `save_gate_a_approval` — this is the analogous seam, one
level over in the portal bridge.
"""

from __future__ import annotations

import pytest


class TestPortalLinkFromDict:
    def test_round_trips_through_to_dict(self) -> None:
        from coord.portal_store import PortalLink

        link = PortalLink(
            repo_name="acme-portal",
            milestone_number=3,
            submission_id="sub_abc123",
            linked_at=1000.0,
            actor="john",
        )
        again = PortalLink.from_dict(link.to_dict())
        assert again == link

    def test_rejects_not_a_dict(self) -> None:
        from coord.portal_store import PortalLink

        assert PortalLink.from_dict(None) is None
        assert PortalLink.from_dict("sub_abc123") is None

    def test_rejects_missing_repo_name(self) -> None:
        from coord.portal_store import PortalLink

        assert PortalLink.from_dict(
            {"milestone_number": 3, "submission_id": "sub_1"}
        ) is None

    def test_rejects_missing_milestone_number(self) -> None:
        from coord.portal_store import PortalLink

        assert PortalLink.from_dict(
            {"repo_name": "acme-portal", "submission_id": "sub_1"}
        ) is None

    def test_rejects_missing_submission_id(self) -> None:
        from coord.portal_store import PortalLink

        assert PortalLink.from_dict(
            {"repo_name": "acme-portal", "milestone_number": 3}
        ) is None

    def test_rejects_a_newer_schema(self) -> None:
        from coord.portal_store import PortalLink

        assert PortalLink.from_dict(
            {
                "repo_name": "acme-portal",
                "milestone_number": 3,
                "submission_id": "sub_1",
                "schema": 999,
            }
        ) is None

    def test_tolerates_a_stringy_milestone_number(self) -> None:
        from coord.portal_store import PortalLink

        link = PortalLink.from_dict(
            {
                "repo_name": "acme-portal",
                "milestone_number": "3",
                "submission_id": "sub_1",
            }
        )
        assert link is not None
        assert link.milestone_number == 3


class TestLinkMilestone:
    def test_link_then_get(self, coord_db) -> None:
        from coord.portal_store import get_milestone_link, link_milestone

        link_milestone(
            repo_name="acme-portal",
            milestone_number=3,
            submission_id="sub_abc123",
            actor="john",
            now=1000.0,
        )
        found = get_milestone_link(repo_name="acme-portal", milestone_number=3)
        assert found is not None
        assert found.submission_id == "sub_abc123"
        assert found.actor == "john"
        assert found.linked_at == 1000.0

    def test_get_returns_none_when_unlinked(self, coord_db) -> None:
        from coord.portal_store import get_milestone_link

        assert get_milestone_link(repo_name="acme-portal", milestone_number=3) is None

    def test_relink_overwrites_not_appends(self, coord_db) -> None:
        from coord.portal_store import (
            get_milestone_link,
            link_milestone,
            list_milestone_links,
        )

        link_milestone(
            repo_name="acme-portal", milestone_number=3, submission_id="sub_typo"
        )
        link_milestone(
            repo_name="acme-portal", milestone_number=3, submission_id="sub_fixed"
        )
        links = [
            link
            for link in list_milestone_links()
            if link.repo_name == "acme-portal" and link.milestone_number == 3
        ]
        assert len(links) == 1
        assert links[0].submission_id == "sub_fixed"
        assert (
            get_milestone_link(repo_name="acme-portal", milestone_number=3).submission_id
            == "sub_fixed"
        )

    def test_different_milestones_coexist(self, coord_db) -> None:
        from coord.portal_store import get_milestone_link, link_milestone

        link_milestone(
            repo_name="acme-portal", milestone_number=3, submission_id="sub_ms3"
        )
        link_milestone(
            repo_name="acme-portal", milestone_number=9, submission_id="sub_ms9"
        )
        assert (
            get_milestone_link(repo_name="acme-portal", milestone_number=3).submission_id
            == "sub_ms3"
        )
        assert (
            get_milestone_link(repo_name="acme-portal", milestone_number=9).submission_id
            == "sub_ms9"
        )

    def test_different_repos_do_not_collide(self, coord_db) -> None:
        from coord.portal_store import get_milestone_link, link_milestone

        link_milestone(repo_name="repo-a", milestone_number=3, submission_id="sub_a")
        link_milestone(repo_name="repo-b", milestone_number=3, submission_id="sub_b")
        assert (
            get_milestone_link(repo_name="repo-a", milestone_number=3).submission_id
            == "sub_a"
        )
        assert (
            get_milestone_link(repo_name="repo-b", milestone_number=3).submission_id
            == "sub_b"
        )


class TestStatePersistenceDirect:
    """The board_meta seam itself — `coord.state`'s half, exercised the same
    way `TestPersistence` in `tests/test_gate_a.py` exercises the sibling
    `gate_a_approvals` seam."""

    def test_record_without_a_key_is_rejected(self, coord_db) -> None:
        from coord.state import _save_portal_link_local

        with pytest.raises(ValueError):
            _save_portal_link_local({"submission_id": "sub_1"})

    def test_list_is_empty_by_default(self, coord_db) -> None:
        from coord.state import list_portal_links

        assert list_portal_links() == []
