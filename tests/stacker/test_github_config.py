from __future__ import annotations

import json
import subprocess
from pathlib import Path

from project_manager.paths import Paths
from project_manager.stacker import gh
from project_manager.stacker.db import StackerDB
from project_manager.stacker.models import PRState, TrackedBranch
from project_manager.stacker.pr.refresh import refresh_review_state
from project_manager.stacker.pr_backend import GhCliBackend
from project_manager.stacker.service import StackerService

from .fakes import RecordingPRBackend


def test_ctx_scopes_backend_to_expanded_repo_config(
    pm_env: Paths,
    monkeypatch,
    tmp_path: Path,
) -> None:
    backend = RecordingPRBackend()
    service = StackerService(StackerDB(pm_env.stacker_db()), pm_env, pr_backend=backend)
    monkeypatch.setenv("PM_TEST_GH_ROOT", str(tmp_path))
    service.set_config("demo", "github.config-dir", "$PM_TEST_GH_ROOT/demo")

    assert service.ctx.pr_backend_for("demo") is backend
    assert backend.scoped_envs == [{"GH_CONFIG_DIR": str(tmp_path / "demo")}]
    assert "GH_TOKEN" not in backend.scoped_envs[0]
    assert "GITHUB_TOKEN" not in backend.scoped_envs[0]


def test_ctx_preserves_unscoped_backend_when_config_is_unset(pm_env: Paths) -> None:
    backend = RecordingPRBackend()
    service = StackerService(StackerDB(pm_env.stacker_db()), pm_env, pr_backend=backend)

    assert service.ctx.pr_backend_for("demo") is backend
    assert backend.scoped_envs == []


def test_review_refresh_scopes_batches_by_pm_repo(pm_env: Paths, tmp_path: Path) -> None:
    backend = RecordingPRBackend()
    db = StackerDB(pm_env.stacker_db())
    service = StackerService(db, pm_env, pr_backend=backend)
    keys = {
        "alpha": ("upstream", "alpha", 1),
        "beta": ("upstream", "beta", 2),
    }
    branches: list[TrackedBranch] = []
    for repo_name, parsed in keys.items():
        config_dir = str(tmp_path / f"gh-{repo_name}")
        db.set_config(repo_name, "github.config-dir", config_dir)
        branches.append(
            TrackedBranch(
                repo_name=repo_name,
                branch="stack/test",
                parent_repo_name=repo_name,
                parent_branch="main",
                managed_base_commit="base",
                last_synced_parent_commit="base",
                last_clean_head="head",
            )
        )
        db.upsert_pr_state(
            PRState(
                repo_name=repo_name,
                branch="stack/test",
                pr_url=f"https://github.com/{parsed[0]}/{parsed[1]}/pull/{parsed[2]}",
                state="OPEN",
            )
        )
        backend.review_by_pr[parsed] = gh.PRReviewSummary(
            state="OPEN",
            is_draft=False,
            is_approved=False,
            has_open_comments=False,
        )

    refresh_review_state(service.ctx, branches)

    assert backend.scoped_envs == [
        {"GH_CONFIG_DIR": str(tmp_path / "gh-alpha")},
        {"GH_CONFIG_DIR": str(tmp_path / "gh-beta")},
    ]
    assert backend.review_calls == [[keys["alpha"]], [keys["beta"]]]


def test_gh_cli_backend_passes_scoped_env_to_every_command(monkeypatch, tmp_path: Path) -> None:
    scoped_env = {"GH_CONFIG_DIR": str(tmp_path / "gh")}
    seen_envs: list[dict[str, str] | None] = []

    def fake_run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        seen_envs.append(kwargs.get("env"))
        stdout = ""
        joined = " ".join(cmd)
        if cmd[1:3] == ["repo", "view"]:
            stdout = json.dumps({"name": "widgets", "owner": {"login": "acme"}})
        elif cmd[1:3] == ["pr", "list"]:
            stdout = "[]"
        elif cmd[1:3] == ["pr", "create"]:
            stdout = "https://github.com/acme/widgets/pull/1\n"
        elif cmd[1:3] == ["pr", "edit"]:
            stdout = ""
        elif "--method POST" in joined:
            stdout = json.dumps({"html_url": "https://github.com/acme/widgets/pull/2"})
        elif cmd[1:3] == ["api", "graphql"] and "search(query:" in joined:
            stdout = json.dumps({"data": {"search": {"nodes": []}}})
        elif cmd[1:3] == ["api", "graphql"]:
            stdout = json.dumps(
                {
                    "data": {
                        "repository": {
                            "pr_1": {
                                "state": "OPEN",
                                "isDraft": False,
                                "merged": False,
                                "latestReviews": {"nodes": []},
                            }
                        }
                    }
                }
            )
        elif cmd[1] == "api" and "/pulls/1" in cmd[2]:
            stdout = json.dumps(
                {
                    "number": 1,
                    "html_url": "https://github.com/acme/widgets/pull/1",
                    "state": "open",
                    "head": {"ref": "feature"},
                    "base": {"ref": "main"},
                }
            )
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(gh, "run", fake_run)
    backend = GhCliBackend().scoped(env=scoped_env)
    body_file = tmp_path / "body.md"
    body_file.write_text("body")

    backend.repo_info(repo="acme/widgets")
    backend.list_open_prs("acme/widgets")
    backend.create_pr(
        gh.CreatePRRequest(
            repo="acme/widgets",
            base="main",
            head="feature",
            title="Feature",
            body_file=body_file,
            draft=False,
        )
    )
    backend.create_pr(
        gh.CreatePRRequest(
            repo="acme/widgets",
            base="main",
            head="feature",
            title="Feature",
            body_file=body_file,
            draft=False,
            head_repo="acme/widgets-fork",
        )
    )
    backend.edit_pr(gh.EditPRRequest(repo="acme/widgets", number=1, title="New title"))
    backend.view_pr("https://github.com/acme/widgets/pull/1")
    backend.search_prs("repo:acme/widgets head:feature is:pr is:open")
    backend.batch_pr_review([("acme", "widgets", 1)])

    assert seen_envs == [scoped_env] * 8
