"""GitHub PR template discovery and commit-body injection.

On PR creation, load the repo's `.github/PULL_REQUEST_TEMPLATE.md` from
the worktree and inject the first commit's body into the template's
first `## ` section, so a `## Summary` heading auto-fills with the
commit body.

Future work (deliberately out of scope for v1): root-level
`PULL_REQUEST_TEMPLATE.md`, `docs/PULL_REQUEST_TEMPLATE.md`, and the
`.github/PULL_REQUEST_TEMPLATE/` multi-template directory.
"""

from __future__ import annotations

from pathlib import Path

TEMPLATE_PATHS: tuple[str, ...] = (
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/pull_request_template.md",
)


def load_pr_template(worktree_path: Path) -> str | None:
    """Return PR template text from `worktree_path`, or None if absent.

    Searches `TEMPLATE_PATHS` in order; first hit wins. UnicodeDecodeError
    or OSError → None (treat as absent). HTML comments are preserved verbatim
    so the template author's instructional `<!-- delete if N/A -->` notes
    survive into the rendered PR body.
    """
    for relative in TEMPLATE_PATHS:
        candidate = worktree_path / relative
        try:
            return candidate.read_text(encoding="utf-8")
        except (FileNotFoundError, IsADirectoryError):
            continue
        except (UnicodeDecodeError, OSError):
            return None
    return None


def inject_body_into_template(commit_body: str, template: str) -> str:
    """Insert `commit_body` into the first `## ` section of `template`.

    - empty `commit_body` → template returned unchanged
    - no `## ` header in template → `commit_body` + blank line + template
    - else → `commit_body` inserted between the first `## ` line and the
      next `## ` line (or end of template); pre-existing prose in the
      first section is preserved AFTER the injected body, matching
      gitstack's behavior so default scaffolding stays visible for the
      user to delete.
    """
    if not commit_body:
        return template
    lines = template.split("\n")
    first_header = next(
        (i for i, line in enumerate(lines) if line.startswith("## ")),
        None,
    )
    if first_header is None:
        return f"{commit_body}\n\n{template}"
    next_header = next(
        (
            i
            for i in range(first_header + 1, len(lines))
            if lines[i].startswith("## ")
        ),
        len(lines),
    )
    out = [
        *lines[: first_header + 1],
        "",
        commit_body,
        "",
        *lines[first_header + 1 : next_header],
        *lines[next_header:],
    ]
    return "\n".join(out)
