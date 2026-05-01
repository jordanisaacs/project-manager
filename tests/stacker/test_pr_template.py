from __future__ import annotations

from pathlib import Path

from project_manager.stacker.pr.template import (
    inject_body_into_template,
    load_pr_template,
)


def test_load_pr_template_uppercase_path(tmp_path: Path) -> None:
    template = "## Summary\n\n## Test plan\n"
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "PULL_REQUEST_TEMPLATE.md").write_text(template)
    assert load_pr_template(tmp_path) == template


def test_load_pr_template_lowercase_fallback(tmp_path: Path) -> None:
    template = "Plain template body\n"
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "pull_request_template.md").write_text(template)
    assert load_pr_template(tmp_path) == template


def test_load_pr_template_uppercase_takes_precedence(tmp_path: Path) -> None:
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "PULL_REQUEST_TEMPLATE.md").write_text("UPPER")
    (tmp_path / ".github" / "pull_request_template.md").write_text("lower")
    assert load_pr_template(tmp_path) == "UPPER"


def test_load_pr_template_absent_returns_none(tmp_path: Path) -> None:
    assert load_pr_template(tmp_path) is None


def test_load_pr_template_unreadable_returns_none(tmp_path: Path) -> None:
    (tmp_path / ".github").mkdir()
    # Non-UTF-8 bytes — invalid as template; treat as absent.
    (tmp_path / ".github" / "PULL_REQUEST_TEMPLATE.md").write_bytes(b"\xff\xfe\x00\x00")
    assert load_pr_template(tmp_path) is None


def test_inject_body_into_template_first_section_filled() -> None:
    template = "## Summary\n\n## Test plan\n"
    body = "Adds frobber"
    out = inject_body_into_template(body, template)
    assert "## Summary" in out
    assert "## Test plan" in out
    summary_index = out.index("## Summary")
    body_index = out.index(body)
    test_plan_index = out.index("## Test plan")
    assert summary_index < body_index < test_plan_index


def test_inject_body_into_template_no_headers_prepends() -> None:
    template = "Boilerplate prose\n"
    body = "Adds frobber"
    out = inject_body_into_template(body, template)
    assert out == f"{body}\n\n{template}"


def test_inject_body_into_template_empty_body_returns_template_verbatim() -> None:
    template = "## Summary\n\n## Test plan\n"
    assert inject_body_into_template("", template) == template


def test_inject_body_into_template_preserves_html_comments() -> None:
    template = "## Summary\n<!-- delete this section if N/A -->\n\n## Test plan\n"
    out = inject_body_into_template("X", template)
    assert "<!-- delete this section if N/A -->" in out


def test_inject_body_into_template_preserves_first_section_default_prose() -> None:
    template = "## Summary\n\nDelete me.\n\n## Other\n"
    out = inject_body_into_template("X", template)
    x_index = out.index("X")
    delete_me_index = out.index("Delete me.")
    other_index = out.index("## Other")
    assert x_index < delete_me_index < other_index
