from __future__ import annotations

from stacker.selectors import parse_selector


def test_parse_selector_unqualified() -> None:
    parsed = parse_selector("feature")
    assert parsed.repo_query is None
    assert parsed.branch == "feature"
    assert parsed.is_root is False


def test_parse_selector_qualified_root() -> None:
    parsed = parse_selector("repo:")
    assert parsed.repo_query == "repo"
    assert parsed.branch is None
    assert parsed.is_root is True
