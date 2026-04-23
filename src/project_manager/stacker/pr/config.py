from __future__ import annotations

from typing import TYPE_CHECKING

from project_manager.stacker import config_schema, git
from project_manager.stacker.models import RepoPRConfig

from .resolve import push_remote_slug

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


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
