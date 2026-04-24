from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from project_manager.paths import Paths
from project_manager.render import Column
from project_manager.stacker import config_schema, git
from project_manager.stacker.db import StackerDB
from project_manager.stacker.models import RepoPRConfig

from .resolve import push_remote_slug

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


@dataclass(frozen=True)
class ConfigRow:
    key: str
    value: str


CONFIG_COLUMNS: list[Column] = [
    Column("Key", "key", style="cyan"),
    Column("Value", "value"),
]


def pr_config(ctx: StackerCtx, repo_name: str) -> RepoPRConfig:
    mode = config_schema.parse_mode(ctx.db.get_config(repo_name, config_schema.PR_MODE))
    trunk = ctx.db.get_config(repo_name, config_schema.PR_TRUNK)
    target = ctx.db.get_config(repo_name, config_schema.PR_TARGET_REPO)
    return RepoPRConfig(
        repo_name=repo_name,
        mode=mode,
        trunk_branch=trunk or git.guess_trunk_branch(ctx.paths.repo(repo_name)),
        target_repo=target or push_remote_slug(ctx, repo_name),
    )


def set_config(ctx: StackerCtx, repo_name: str, key: str, value: str) -> list[str]:
    """Write one config key, enforcing cross-key invariants.

    Returns informational notices (e.g. auto-flip of pr.mode) for display.
    Only hits `gh repo view` when the key being set requires cross-check
    against the push remote.
    """
    config_schema.validate_value(key, value)
    notices: list[str] = []
    if key == config_schema.PR_MODE and value == "pr-pr":
        push_remote = push_remote_slug(ctx, repo_name)
        target = (
            ctx.db.get_config(repo_name, config_schema.PR_TARGET_REPO)
            or push_remote
        )
        if target != push_remote:
            raise git.GitError(
                f"pr.mode=pr-pr requires pr.target-repo to equal the push "
                f"remote ({push_remote}), but pr.target-repo is {target}. "
                "Unset pr.target-repo first or switch to pr.mode=repo-pr."
            )
    ctx.db.set_config(repo_name, key, value)
    if key == config_schema.PR_TARGET_REPO:
        push_remote = push_remote_slug(ctx, repo_name)
        if value != push_remote:
            current_mode = config_schema.parse_mode(
                ctx.db.get_config(repo_name, config_schema.PR_MODE)
            )
            if current_mode == "pr-pr":
                ctx.db.set_config(repo_name, config_schema.PR_MODE, "repo-pr")
                notices.append(
                    "Switched pr.mode to repo-pr because pr.target-repo "
                    f"({value}) differs from the push remote ({push_remote})."
                )
    return notices


def get_config(ctx: StackerCtx, repo_name: str, key: str) -> str | None:
    config_schema.require_known_key(key)
    return ctx.db.get_config(repo_name, key)


def unset_config(ctx: StackerCtx, repo_name: str, key: str) -> bool:
    config_schema.require_known_key(key)
    return ctx.db.unset_config(repo_name, key)


def list_config(ctx: StackerCtx, repo_name: str) -> list[tuple[str, str]]:
    return ctx.db.list_config(repo_name)


def configured_trunk(paths: Paths, repo_name: str) -> str | None:
    """Return stacker's configured pr.trunk for a repo, or None.

    Public read-only accessor for callers outside the stacker subsystem
    (e.g. `pm repo ls`) that want to know the merge target without having
    to construct a full StackerCtx. Returns None when the stacker DB
    doesn't exist yet or the key is unset — the caller decides how to
    fall back.
    """
    db_path = paths.stacker_db()
    if not db_path.exists():
        return None
    return StackerDB(db_path).get_config(repo_name, config_schema.PR_TRUNK)


def configured_trunks(paths: Paths) -> dict[str, str]:
    """Read pr.trunk for every repo in a single SQLite query.

    `pm repo ls` calls this once per invocation so it doesn't reopen the
    stacker DB N times. Returns an empty dict when the DB is absent.
    """
    db_path = paths.stacker_db()
    if not db_path.exists():
        return {}
    with StackerDB(db_path).connect() as conn:
        rows = conn.execute(
            "SELECT repo_name, value FROM config WHERE key = ?",
            (config_schema.PR_TRUNK,),
        ).fetchall()
    return {row["repo_name"]: row["value"] for row in rows}
