from __future__ import annotations

import json
from pathlib import Path

from conftest import cli, commit_file, configure_gh_repo, read_fake_prs, run


def configure_remote_with_pp_alias(repo: Path, env: dict[str, str]) -> Path:
    remote = repo.parent / f"{repo.name}-origin.git"
    assert run(["git", "init", "--bare", str(remote)], env=env).returncode == 0
    assert run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], env=env).returncode == 0
    assert run(["git", "-C", str(repo), "push", "-u", "origin", "main"], env=env).returncode == 0
    assert (
        run(
            ["git", "-C", str(repo), "config", "alias.pp", "!git push -u origin HEAD"],
            env=env,
        ).returncode
        == 0
    )
    configure_gh_repo(repo, env, f"stacker/{repo.name}")
    return remote


def test_create_b_registers_child(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    proc = cli(stacker_env, "create", "-b", "child", cwd=repo)
    assert proc.returncode == 0, proc.stderr

    status = cli(stacker_env, "status", "child", cwd=repo)
    assert status.returncode == 0
    assert "parent: demo:main" in status.stdout


def test_parent_prints_managed_base_commit(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    proc = cli(stacker_env, "create", "-b", "child", cwd=repo)
    assert proc.returncode == 0, proc.stderr

    expected = run(["git", "-C", str(repo), "rev-parse", "HEAD"], env=stacker_env).stdout.strip()
    parent = cli(stacker_env, "parent", "child", cwd=repo)
    assert parent.returncode == 0, parent.stderr
    assert parent.stdout.strip() == expected


def test_create_with_wrapper_result_file_still_tracks(repo_factory, stacker_env, tmp_path: Path) -> None:
    repo = repo_factory("demo")
    wrapper_result = tmp_path / "wrapper-result.txt"
    proc = cli(stacker_env, "create", "--result-file", str(wrapper_result), "-b", "child", cwd=repo)
    assert proc.returncode == 0, proc.stderr
    child_path = Path(wrapper_result.read_text().strip())
    assert child_path.is_dir()

    status = cli(stacker_env, "status", "child", cwd=repo)
    assert status.returncode == 0, status.stderr
    assert "parent: demo:main" in status.stdout


def test_create_base_registers_specific_parent(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    first = cli(stacker_env, "create", "-b", "base", cwd=repo)
    assert first.returncode == 0, first.stderr
    base_path = Path(first.stdout.strip())
    commit_file(base_path, stacker_env, "file.txt", "base\n", "base change")

    second = cli(stacker_env, "create", "-B", "base", "child", cwd=repo)
    assert second.returncode == 0, second.stderr
    status = cli(stacker_env, "status", "child", cwd=repo)
    assert "parent: demo:base" in status.stdout


def test_create_base_accepts_repo_qualified_parent(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    first = cli(stacker_env, "create", "-b", "base", cwd=repo)
    assert first.returncode == 0, first.stderr
    base_path = Path(first.stdout.strip())
    commit_file(base_path, stacker_env, "file.txt", "base\n", "base change")

    second = cli(stacker_env, "create", "-B", "demo:base", "child", cwd=repo)
    assert second.returncode == 0, second.stderr
    status = cli(stacker_env, "status", "child", cwd=repo)
    assert "parent: demo:base" in status.stdout


def test_create_interactive_prompt_path(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    proc = cli(stacker_env, "create", "child", cwd=repo, input_text="y\n")
    assert proc.returncode == 0, proc.stderr
    status = cli(stacker_env, "status", "child", cwd=repo)
    assert status.returncode == 0


def test_track_existing_worktree(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    wt_dir = Path(stacker_env["HOME"]) / ".worktrees" / repo.name / "tracked"
    wt_dir.parent.mkdir(parents=True, exist_ok=True)
    assert run(["git", "-C", str(repo), "worktree", "add", "-b", "tracked", str(wt_dir)], env=stacker_env).returncode == 0

    proc = cli(stacker_env, "track", "tracked", "--parent", "main", cwd=repo)
    assert proc.returncode == 0, proc.stderr
    status = cli(stacker_env, "status", "tracked", cwd=repo)
    assert "parent: demo:main" in status.stdout


def test_track_defaults_to_current_worktree(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    created = cli(stacker_env, "create", "-b", "child", cwd=repo)
    assert created.returncode == 0, created.stderr
    child_path = Path(created.stdout.strip())

    proc = cli(stacker_env, "track", "--parent", "main", cwd=child_path)
    assert proc.returncode == 0, proc.stderr
    status = cli(stacker_env, "status", cwd=child_path)
    assert "parent: demo:main" in status.stdout


def test_untrack_explicit_target(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    created = cli(stacker_env, "create", "-b", "child", cwd=repo)
    assert created.returncode == 0, created.stderr

    proc = cli(stacker_env, "untrack", "child", cwd=repo)
    assert proc.returncode == 0, proc.stderr
    assert "Untracked demo:child" in proc.stdout

    status = cli(stacker_env, "status", "child", cwd=repo)
    assert "not tracked" in status.stdout


def test_untrack_defaults_to_current_worktree(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    created = cli(stacker_env, "create", "-b", "child", cwd=repo)
    assert created.returncode == 0, created.stderr
    child_path = Path(created.stdout.strip())

    proc = cli(stacker_env, "untrack", cwd=child_path)
    assert proc.returncode == 0, proc.stderr
    assert "Untracked demo:child" in proc.stdout

    status = cli(stacker_env, "status", cwd=child_path)
    assert "not tracked" in status.stdout


def test_delete_removes_worktree_and_untracks(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    created = cli(stacker_env, "create", "-b", "child", cwd=repo)
    assert created.returncode == 0, created.stderr
    child_path = Path(created.stdout.strip())

    proc = cli(stacker_env, "delete", "child", cwd=repo)
    assert proc.returncode == 0, proc.stderr
    assert "Deleted and untracked demo:child" in proc.stdout
    assert not child_path.exists()

    status = cli(stacker_env, "status", "child", cwd=repo)
    assert "not tracked" in status.stdout


def test_delete_defaults_to_current_worktree(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    created = cli(stacker_env, "create", "-b", "child", cwd=repo)
    assert created.returncode == 0, created.stderr
    child_path = Path(created.stdout.strip())

    proc = cli(stacker_env, "delete", cwd=child_path)
    assert proc.returncode == 0, proc.stderr
    assert "Deleted and untracked demo:child" in proc.stdout
    assert not child_path.exists()


def test_delete_b_also_deletes_branch(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    created = cli(stacker_env, "create", "-b", "child", cwd=repo)
    assert created.returncode == 0, created.stderr
    child_path = Path(created.stdout.strip())

    proc = cli(stacker_env, "delete", "-b", "child", cwd=repo)
    assert proc.returncode == 0, proc.stderr
    assert "Deleted and untracked demo:child" in proc.stdout
    assert not child_path.exists()
    assert run(["git", "-C", str(repo), "rev-parse", "--verify", "child"], env=stacker_env).returncode != 0


def test_untrack_after_worktree_and_branch_deleted(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    first = cli(stacker_env, "create", "-b", "child", cwd=repo)
    assert first.returncode == 0, first.stderr
    child_path = Path(first.stdout.strip())
    second = cli(stacker_env, "create", "-b", "grandchild", cwd=child_path)
    assert second.returncode == 0, second.stderr
    grandchild_path = Path(second.stdout.strip())
    before = cli(stacker_env, "status", cwd=grandchild_path)
    before_base = next(line for line in before.stdout.splitlines() if line.startswith("managed base: "))

    assert run(["git", "-C", str(repo), "worktree", "remove", "--force", str(child_path)], env=stacker_env).returncode == 0
    assert run(["git", "-C", str(repo), "branch", "-D", "child"], env=stacker_env).returncode == 0

    proc = cli(stacker_env, "untrack", "child", cwd=repo)
    assert proc.returncode == 0, proc.stderr
    assert "Untracked demo:child" in proc.stdout

    graph = cli(stacker_env, "graph", cwd=repo)
    assert "demo:child" not in graph.stdout

    status = cli(stacker_env, "status", cwd=grandchild_path)
    assert "parent: demo:main" in status.stdout
    assert before_base in status.stdout


def test_untrack_completion_uses_db_for_deleted_branch(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    created = cli(stacker_env, "create", "-b", "child", cwd=repo)
    assert created.returncode == 0, created.stderr
    child_path = Path(created.stdout.strip())

    assert run(["git", "-C", str(repo), "worktree", "remove", "--force", str(child_path)], env=stacker_env).returncode == 0
    assert run(["git", "-C", str(repo), "branch", "-D", "child"], env=stacker_env).returncode == 0

    completion = cli(stacker_env, "__complete-untrack", "ch", cwd=repo)
    assert completion.returncode == 0, completion.stderr
    assert completion.stdout.splitlines() == ["child"]


def test_untrack_reparents_children_to_grandparent(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    first = cli(stacker_env, "create", "-b", "child", cwd=repo)
    assert first.returncode == 0, first.stderr
    child_path = Path(first.stdout.strip())
    second = cli(stacker_env, "create", "-b", "grandchild", cwd=child_path)
    assert second.returncode == 0, second.stderr
    grandchild_path = Path(second.stdout.strip())
    before = cli(stacker_env, "status", cwd=grandchild_path)
    before_base = next(line for line in before.stdout.splitlines() if line.startswith("managed base: "))

    proc = cli(stacker_env, "untrack", "child", cwd=repo)
    assert proc.returncode == 0, proc.stderr

    status = cli(stacker_env, "status", cwd=grandchild_path)
    assert "parent: demo:main" in status.stdout
    assert before_base in status.stdout

    graph = cli(stacker_env, "graph", cwd=repo)
    assert "demo:child" not in graph.stdout


def test_log_shows_subject_and_author_since_parent(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    created = cli(stacker_env, "create", "-b", "child", cwd=repo)
    assert created.returncode == 0, created.stderr
    child_path = Path(created.stdout.strip())

    commit_file(child_path, stacker_env, "first.txt", "one\n", "first child commit")
    commit_file(child_path, stacker_env, "second.txt", "two\n", "second child commit")

    log = cli(stacker_env, "log", cwd=child_path)
    assert log.returncode == 0, log.stderr
    assert "child commits since parent:" in log.stdout
    assert "first child commit (Stacker Test)" in log.stdout
    assert "second child commit (Stacker Test)" in log.stdout


def test_log_reports_no_commits_since_parent(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    created = cli(stacker_env, "create", "-b", "child", cwd=repo)
    assert created.returncode == 0, created.stderr
    child_path = Path(created.stdout.strip())

    log = cli(stacker_env, "log", cwd=child_path)
    assert log.returncode == 0, log.stderr
    assert "No commits since demo:main at" in log.stdout


def test_graph_marks_current_worktree(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    created = cli(stacker_env, "create", "-b", "child", cwd=repo)
    assert created.returncode == 0, created.stderr
    child_path = Path(created.stdout.strip())

    graph = cli(stacker_env, "graph", cwd=child_path)
    assert graph.returncode == 0, graph.stderr
    assert "demo (" in graph.stdout
    assert "demo:child [synced] [current]" in graph.stdout
    assert "demo:main [untracked root]" in graph.stdout


def test_graph_marks_unsynced_children(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    created = cli(stacker_env, "create", "-b", "child", cwd=repo)
    assert created.returncode == 0, created.stderr

    commit_file(repo, stacker_env, "parent.txt", "parent moved\n", "parent moved")

    graph = cli(stacker_env, "graph", cwd=repo)
    assert graph.returncode == 0, graph.stderr
    assert "demo:child [unsynced]" in graph.stdout


def test_graph_marks_dirty_children(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    created = cli(stacker_env, "create", "-b", "child", cwd=repo)
    assert created.returncode == 0, created.stderr
    child_path = Path(created.stdout.strip())

    (child_path / "file.txt").write_text("dirty\n")

    graph = cli(stacker_env, "graph", cwd=repo)
    assert graph.returncode == 0, graph.stderr
    assert "demo:child [synced] [dirty]" in graph.stdout


def test_graph_handles_tracked_branch_without_worktree_for_dirty_marker(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    created = cli(stacker_env, "create", "-b", "child", cwd=repo)
    assert created.returncode == 0, created.stderr
    child_path = Path(created.stdout.strip())

    assert run(["git", "-C", str(repo), "worktree", "remove", "--force", str(child_path)], env=stacker_env).returncode == 0

    graph = cli(stacker_env, "graph", cwd=repo)
    assert graph.returncode == 0, graph.stderr
    assert "demo:child [synced]" in graph.stdout
    assert "[dirty]" not in graph.stdout


def test_help_has_command_descriptions(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")

    help_out = cli(stacker_env, "--help", cwd=repo)
    assert help_out.returncode == 0, help_out.stderr
    assert "Resumable worktree stack sync for wt-managed Git worktrees." in help_out.stdout
    assert "Selector syntax:" in help_out.stdout
    assert "stacker push" in help_out.stdout

    sync_help = cli(stacker_env, "sync", "--help", cwd=repo)
    assert sync_help.returncode == 0, sync_help.stderr
    assert "Reset the child to the current parent head" in sync_help.stdout
    assert "[repo:]<branch>" in sync_help.stdout


def test_zsh_output_matches_checked_in_file(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")

    zsh_out = cli(stacker_env, "zsh", cwd=repo)
    assert zsh_out.returncode == 0, zsh_out.stderr
    checked_in = (Path(__file__).resolve().parents[1] / "src" / "stacker" / "stacker.zsh").read_text().strip()
    assert zsh_out.stdout.strip() == checked_in


def test_next_and_prev_walk_linear_tree(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    created = cli(stacker_env, "create", "-b", "child", cwd=repo)
    assert created.returncode == 0, created.stderr
    child_path = Path(created.stdout.strip())

    next_from_root = cli(stacker_env, "next", cwd=repo)
    assert next_from_root.returncode == 0, next_from_root.stderr
    assert next_from_root.stdout.strip() == str(child_path)

    prev_from_child = cli(stacker_env, "prev", cwd=child_path)
    assert prev_from_child.returncode == 0, prev_from_child.stderr
    assert prev_from_child.stdout.strip() == str(repo)

    next_rev = cli(stacker_env, "next", "--rev", cwd=repo)
    assert next_rev.returncode == 0, next_rev.stderr
    assert next_rev.stdout.strip() == "child"

    prev_rev = cli(stacker_env, "prev", "--rev", cwd=child_path)
    assert prev_rev.returncode == 0, prev_rev.stderr
    assert prev_rev.stdout.strip() == "main"


def test_next_fails_for_multiple_children(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    first = cli(stacker_env, "create", "-b", "child1", cwd=repo)
    assert first.returncode == 0, first.stderr
    second = cli(stacker_env, "create", "-b", "child2", cwd=repo)
    assert second.returncode == 0, second.stderr

    next_out = cli(stacker_env, "next", cwd=repo)
    assert next_out.returncode == 1
    assert "not implemented" in next_out.stderr.lower()


def test_pp_walks_until_branch_without_upstream(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    configure_remote_with_pp_alias(repo, stacker_env)

    first = cli(stacker_env, "create", "-b", "child1", cwd=repo)
    assert first.returncode == 0, first.stderr
    child1 = Path(first.stdout.strip())
    commit_file(child1, stacker_env, "child1.txt", "one\n", "child1 change")
    assert run(["git", "-C", str(child1), "push", "-u", "origin", "HEAD"], env=stacker_env).returncode == 0

    second = cli(stacker_env, "create", "-b", "child2", cwd=child1)
    assert second.returncode == 0, second.stderr
    child2 = Path(second.stdout.strip())
    commit_file(child2, stacker_env, "child2.txt", "two\n", "child2 change")
    commit_file(child1, stacker_env, "child1b.txt", "one more\n", "child1 followup")

    pushed = cli(stacker_env, "pp", cwd=repo)
    assert pushed.returncode == 0, pushed.stderr
    assert "Running git pp --force in demo:child1" in pushed.stdout
    assert "Stopping at demo:child2: no upstream remote is configured." in pushed.stdout
    assert "Running git pp --force in demo:child2" not in pushed.stdout

    child1_head = run(["git", "-C", str(child1), "rev-parse", "HEAD"], env=stacker_env).stdout.strip()
    remote_head = run(["git", "-C", str(repo), "rev-parse", "origin/child1"], env=stacker_env).stdout.strip()
    assert remote_head == child1_head

    child2_upstream = run(
        ["git", "-C", str(child2), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        env=stacker_env,
    )
    assert child2_upstream.returncode != 0


def test_repo_pr_mode_set_and_show(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    configure_gh_repo(repo, stacker_env, "stacker/demo")

    set_mode = cli(stacker_env, "repo", "set-pr-mode", "--mode", "normal", "--trunk", "main", cwd=repo)
    assert set_mode.returncode == 0, set_mode.stderr

    shown = cli(stacker_env, "repo", "show-pr-mode", cwd=repo)
    assert shown.returncode == 0, shown.stderr
    assert "mode: normal" in shown.stdout
    assert "trunk: main" in shown.stdout
    assert "main repo: stacker/demo" in shown.stdout


def test_repo_pr_mode_rejects_missing_main_repo_for_forked(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    configure_gh_repo(repo, stacker_env, "stacker/demo")

    proc = cli(stacker_env, "repo", "set-pr-mode", "--mode", "forked", "--trunk", "main", cwd=repo)
    assert proc.returncode == 1
    assert "--main-repo is required for forked mode." in proc.stderr


def test_pr_pushes_current_branch_only_and_creates_trunk_pr(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    configure_remote_with_pp_alias(repo, stacker_env)
    first = cli(stacker_env, "create", "-b", "child1", cwd=repo)
    assert first.returncode == 0, first.stderr
    child1 = Path(first.stdout.strip())
    second = cli(stacker_env, "create", "-b", "child2", cwd=child1)
    assert second.returncode == 0, second.stderr
    child2 = Path(second.stdout.strip())
    commit_file(child1, stacker_env, "child1.txt", "one\n", "child1 title\n\nchild1 body")
    commit_file(child2, stacker_env, "child2.txt", "two\n", "child2 title\n\nchild2 body")

    proc = cli(stacker_env, "pr", cwd=child1)
    assert proc.returncode == 0, proc.stderr
    assert "Running git pp --force in demo:child1" in proc.stdout
    assert "PR ready: https://github.com/stacker/demo/pull/1" in proc.stdout

    child1_upstream = run(
        ["git", "-C", str(child1), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        env=stacker_env,
    )
    assert child1_upstream.returncode == 0
    child2_upstream = run(
        ["git", "-C", str(child2), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        env=stacker_env,
    )
    assert child2_upstream.returncode != 0

    prs = read_fake_prs(stacker_env)
    assert len(prs) == 1
    assert prs[0]["title"] == "child1 title"
    assert prs[0]["baseRefName"] == "main"
    assert "child1 body" in prs[0]["body"]
    assert "<!-- stacker:begin -->" in prs[0]["body"]


def test_pr_requires_direct_parent_open_pr_in_normal_mode(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    configure_remote_with_pp_alias(repo, stacker_env)
    first = cli(stacker_env, "create", "-b", "child1", cwd=repo)
    child1 = Path(first.stdout.strip())
    second = cli(stacker_env, "create", "-b", "child2", cwd=child1)
    child2 = Path(second.stdout.strip())
    commit_file(child2, stacker_env, "child2.txt", "two\n", "child2 title")

    proc = cli(stacker_env, "pr", cwd=child2)
    assert proc.returncode == 1
    assert "Direct parent demo:child1 must already have an open PR." in proc.stderr


def test_pr_bases_child_on_direct_parent_and_updates_parent_block(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    configure_remote_with_pp_alias(repo, stacker_env)
    first = cli(stacker_env, "create", "-b", "child1", cwd=repo)
    child1 = Path(first.stdout.strip())
    second = cli(stacker_env, "create", "-b", "child2", cwd=child1)
    child2 = Path(second.stdout.strip())
    commit_file(child1, stacker_env, "child1.txt", "one\n", "child1 title\n\nchild1 body")
    commit_file(child2, stacker_env, "child2.txt", "two\n", "child2 title\n\nchild2 body")

    pr1 = cli(stacker_env, "pr", cwd=child1)
    assert pr1.returncode == 0, pr1.stderr
    pr2 = cli(stacker_env, "pr", cwd=child2)
    assert pr2.returncode == 0, pr2.stderr

    prs = {pr["headRefName"]: pr for pr in read_fake_prs(stacker_env)}
    assert prs["child2"]["baseRefName"] == "child1"
    assert "https://github.com/stacker/demo/pull/2" in prs["child1"]["body"]
    assert "[(child2) ->](https://github.com/stacker/demo/pull/2)" in prs["child1"]["body"]
    child1_base = run(["git", "-C", str(child1), "merge-base", "main", "HEAD"], env=stacker_env).stdout.strip()
    child1_head = run(["git", "-C", str(child1), "rev-parse", "HEAD"], env=stacker_env).stdout.strip()
    assert f"**[child1 (changes)](https://github.com/stacker/demo/compare/{child1_base}...{child1_head})**" in prs["child1"]["body"]


def test_pr_preserves_non_stacker_text_when_refreshing_block(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    configure_remote_with_pp_alias(repo, stacker_env)
    child = cli(stacker_env, "create", "-b", "child", cwd=repo)
    child_path = Path(child.stdout.strip())
    commit_file(child_path, stacker_env, "child.txt", "one\n", "child title\n\nchild body")

    first = cli(stacker_env, "pr", cwd=child_path)
    assert first.returncode == 0, first.stderr

    state_path = Path(stacker_env["XDG_STATE_HOME"]) / "gh-prs.json"
    prs = json.loads(state_path.read_text())
    prs[0]["body"] = "User note\n\n<!-- stacker:begin -->\nstale\n<!-- stacker:end -->"
    state_path.write_text(json.dumps(prs))

    second = cli(stacker_env, "pr", cwd=child_path)
    assert second.returncode == 0, second.stderr
    updated = read_fake_prs(stacker_env)[0]["body"]
    assert updated.startswith("User note")
    assert "stale" not in updated
    assert "<!-- stacker:begin -->" in updated


def test_pr_forked_mode_targets_main_repo_and_writes_stack_block(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    configure_remote_with_pp_alias(repo, stacker_env)
    configure_gh_repo(repo, stacker_env, "dev/demo")
    child = cli(stacker_env, "create", "-b", "child", cwd=repo)
    child_path = Path(child.stdout.strip())
    commit_file(child_path, stacker_env, "child.txt", "one\n", "child title\n\nchild body")
    set_mode = cli(
        stacker_env,
        "repo",
        "set-pr-mode",
        "--mode",
        "forked",
        "--trunk",
        "main",
        "--main-repo",
        "main/demo",
        cwd=repo,
    )
    assert set_mode.returncode == 0, set_mode.stderr

    proc = cli(stacker_env, "pr", cwd=child_path)
    assert proc.returncode == 0, proc.stderr

    prs = read_fake_prs(stacker_env)
    assert len(prs) == 1
    assert prs[0]["repo"] == "main/demo"
    assert prs[0]["headSearch"] == "dev:child"
    assert prs[0]["baseRefName"] == "main"
    child_base = run(["git", "-C", str(child_path), "merge-base", "main", "HEAD"], env=stacker_env).stdout.strip()
    child_head = run(["git", "-C", str(child_path), "rev-parse", "HEAD"], env=stacker_env).stdout.strip()
    assert f"https://github.com/main/demo/compare/{child_base}...{child_head}" in prs[0]["body"]


def test_pp_does_not_refresh_pr_blocks_in_normal_mode(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    configure_remote_with_pp_alias(repo, stacker_env)
    first = cli(stacker_env, "create", "-b", "child1", cwd=repo)
    child1 = Path(first.stdout.strip())
    second = cli(stacker_env, "create", "-b", "child2", cwd=child1)
    child2 = Path(second.stdout.strip())
    commit_file(child1, stacker_env, "child1.txt", "one\n", "child1 title\n\nchild1 body")
    create_pr = cli(stacker_env, "pr", cwd=child1)
    assert create_pr.returncode == 0, create_pr.stderr

    state_path = Path(stacker_env["XDG_STATE_HOME"]) / "gh-prs.json"
    prs = json.loads(state_path.read_text())
    prs[0]["body"] = "User note\n\n<!-- stacker:begin -->\nstale\n<!-- stacker:end -->"
    state_path.write_text(json.dumps(prs))

    commit_file(child1, stacker_env, "child1b.txt", "more\n", "child1 followup")
    commit_file(child2, stacker_env, "child2.txt", "two\n", "child2 title")

    proc = cli(stacker_env, "pp", cwd=repo)
    assert proc.returncode == 0, proc.stderr
    updated = read_fake_prs(stacker_env)
    assert len(updated) == 1
    assert "stale" in updated[0]["body"]
    assert "Updated stack block" not in proc.stdout


def test_pp_refreshes_pr_blocks_once_in_forked_mode(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    configure_remote_with_pp_alias(repo, stacker_env)
    configure_gh_repo(repo, stacker_env, "dev/demo")

    first = cli(stacker_env, "create", "-b", "child1", cwd=repo)
    assert first.returncode == 0, first.stderr
    child1 = Path(first.stdout.strip())
    second = cli(stacker_env, "create", "-b", "child2", cwd=child1)
    assert second.returncode == 0, second.stderr
    child2 = Path(second.stdout.strip())

    commit_file(child1, stacker_env, "child1.txt", "one\n", "child1 title\n\nchild1 body")
    commit_file(child2, stacker_env, "child2.txt", "two\n", "child2 title\n\nchild2 body")

    set_mode = cli(
        stacker_env,
        "repo",
        "set-pr-mode",
        "--mode",
        "forked",
        "--trunk",
        "main",
        "--main-repo",
        "main/demo",
        cwd=repo,
    )
    assert set_mode.returncode == 0, set_mode.stderr

    pr1 = cli(stacker_env, "pr", cwd=child1)
    assert pr1.returncode == 0, pr1.stderr
    pr2 = cli(stacker_env, "pr", cwd=child2)
    assert pr2.returncode == 0, pr2.stderr

    state_path = Path(stacker_env["XDG_STATE_HOME"]) / "gh-prs.json"
    prs = json.loads(state_path.read_text())
    for pr in prs:
        pr["body"] = "User note\n\n<!-- stacker:begin -->\nstale\n<!-- stacker:end -->"
    state_path.write_text(json.dumps(prs))

    commit_file(child1, stacker_env, "child1b.txt", "more\n", "child1 followup")
    commit_file(child2, stacker_env, "child2b.txt", "more\n", "child2 followup")

    proc = cli(stacker_env, "pp", cwd=repo)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.count("Updated stack block for PR #") == 2

    updated = read_fake_prs(stacker_env)
    assert len(updated) == 2
    assert all("stale" not in pr["body"] for pr in updated)


def test_pp_stack_block_can_refresh_normal_mode_on_request(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    configure_remote_with_pp_alias(repo, stacker_env)
    child = cli(stacker_env, "create", "-b", "child", cwd=repo)
    assert child.returncode == 0, child.stderr
    child_path = Path(child.stdout.strip())
    commit_file(child_path, stacker_env, "child.txt", "one\n", "child title\n\nchild body")

    create_pr = cli(stacker_env, "pr", cwd=child_path)
    assert create_pr.returncode == 0, create_pr.stderr

    state_path = Path(stacker_env["XDG_STATE_HOME"]) / "gh-prs.json"
    prs = json.loads(state_path.read_text())
    prs[0]["body"] = "User note\n\n<!-- stacker:begin -->\nstale\n<!-- stacker:end -->"
    state_path.write_text(json.dumps(prs))

    commit_file(child_path, stacker_env, "child2.txt", "two\n", "child followup")
    proc = cli(stacker_env, "pp", "--stack-block", cwd=repo)
    assert proc.returncode == 0, proc.stderr
    assert "Updated stack block for PR #1 (demo:child)" in proc.stdout

    updated = read_fake_prs(stacker_env)
    assert len(updated) == 1
    assert "stale" not in updated[0]["body"]


def test_pp_erase_stack_block_removes_managed_block(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    configure_remote_with_pp_alias(repo, stacker_env)
    child = cli(stacker_env, "create", "-b", "child", cwd=repo)
    assert child.returncode == 0, child.stderr
    child_path = Path(child.stdout.strip())
    commit_file(child_path, stacker_env, "child.txt", "one\n", "child title\n\nchild body")

    create_pr = cli(stacker_env, "pr", cwd=child_path)
    assert create_pr.returncode == 0, create_pr.stderr

    proc = cli(stacker_env, "pp", "--erase-stack-block", cwd=repo)
    assert proc.returncode == 0, proc.stderr
    assert "Erased stack block for PR #1 (demo:child)" in proc.stdout

    updated = read_fake_prs(stacker_env)
    assert len(updated) == 1
    assert "<!-- stacker:begin -->" not in updated[0]["body"]
    assert "child body" in updated[0]["body"]


def test_forked_stack_block_renders_branched_tree_without_repo_prefix(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    configure_remote_with_pp_alias(repo, stacker_env)
    configure_gh_repo(repo, stacker_env, "dev/demo")
    set_mode = cli(
        stacker_env,
        "repo",
        "set-pr-mode",
        "--mode",
        "forked",
        "--trunk",
        "main",
        "--main-repo",
        "main/demo",
        cwd=repo,
    )
    assert set_mode.returncode == 0, set_mode.stderr

    child1 = Path(cli(stacker_env, "create", "-b", "child1", cwd=repo).stdout.strip())
    child2 = Path(cli(stacker_env, "create", "-b", "child2", cwd=child1).stdout.strip())
    child3 = Path(cli(stacker_env, "create", "-b", "child3", cwd=child1).stdout.strip())
    commit_file(child1, stacker_env, "child1.txt", "one\n", "child1 title\n\nchild1 body")
    commit_file(child2, stacker_env, "child2.txt", "two\n", "child2 title\n\nchild2 body")
    commit_file(child3, stacker_env, "child3.txt", "three\n", "child3 title\n\nchild3 body")

    assert cli(stacker_env, "pr", cwd=child1).returncode == 0
    assert cli(stacker_env, "pr", cwd=child2).returncode == 0
    assert cli(stacker_env, "pr", cwd=child3).returncode == 0

    proc = cli(stacker_env, "pp", cwd=repo)
    assert proc.returncode == 0, proc.stderr

    prs = {pr["headRefName"]: pr for pr in read_fake_prs(stacker_env)}
    body = prs["child1"]["body"]
    assert "<strong><code>child1</code></strong>" in body
    assert "`child2`" in body
    assert "`child3`" in body
    assert "- **<strong><code>child1</code></strong> (current) [PR #1](https://github.com/main/demo/pull/1) [changes](" in body
    assert "demo:child1" not in body
    assert "demo:child2" not in body
    assert "demo:child3" not in body


def test_sync_success(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    child = cli(stacker_env, "create", "-b", "child", cwd=repo)
    assert child.returncode == 0, child.stderr
    child_path = Path(child.stdout.strip())
    commit_file(child_path, stacker_env, "child.txt", "child\n", "child change")
    commit_file(repo, stacker_env, "file.txt", "root changed\n", "root change")

    proc = cli(stacker_env, "sync", "child", cwd=repo)
    assert proc.returncode == 0, proc.stderr
    assert "Resetting demo:child to demo:main" in proc.stdout
    assert "Cherry-picking" in proc.stdout
    log = run(["git", "-C", str(child_path), "log", "--oneline", "--decorate", "-n", "3"], env=stacker_env)
    assert "root change" in log.stdout
    assert "child change" in log.stdout


def test_sync_noop_when_parent_unchanged(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    child = cli(stacker_env, "create", "-b", "child", cwd=repo)
    assert child.returncode == 0, child.stderr
    child_path = Path(child.stdout.strip())
    commit_file(child_path, stacker_env, "child.txt", "child\n", "child change")

    proc = cli(stacker_env, "sync", "child", cwd=repo)
    assert proc.returncode == 0, proc.stderr
    assert "Nothing to sync for demo:child." in proc.stdout
    assert "Parent demo:main is unchanged at" in proc.stdout


def test_repair_restamps_metadata_to_explicit_base(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    child = cli(stacker_env, "create", "-b", "child", cwd=repo)
    assert child.returncode == 0, child.stderr
    child_path = Path(child.stdout.strip())
    commit_file(child_path, stacker_env, "child.txt", "child\n", "child change")

    commit_file(repo, stacker_env, "parent1.txt", "one\n", "parent one")
    first_sync = cli(stacker_env, "sync", cwd=child_path)
    assert first_sync.returncode == 0, first_sync.stderr

    before = cli(stacker_env, "status", cwd=child_path)
    before_base = next(line.split(": ", 1)[1] for line in before.stdout.splitlines() if line.startswith("managed base: "))
    child_commit = run(["git", "-C", str(child_path), "rev-parse", "HEAD"], env=stacker_env).stdout.strip()

    commit_file(repo, stacker_env, "parent2.txt", "two\n", "parent two")
    new_parent_head = run(["git", "-C", str(repo), "rev-parse", "HEAD"], env=stacker_env).stdout.strip()

    assert run(["git", "-C", str(child_path), "reset", "--hard", new_parent_head], env=stacker_env).returncode == 0
    assert run(["git", "-C", str(child_path), "cherry-pick", child_commit], env=stacker_env).returncode == 0

    stale = cli(stacker_env, "status", cwd=child_path)
    assert f"managed base: {before_base}" in stale.stdout

    repaired = cli(stacker_env, "repair", "--base", new_parent_head, cwd=child_path)
    assert repaired.returncode == 0, repaired.stderr
    assert "Repaired demo:child." in repaired.stdout
    assert f"Stored base ref: {new_parent_head}"[:24] in repaired.stdout
    assert new_parent_head[:12] in repaired.stdout

    after = cli(stacker_env, "status", cwd=child_path)
    assert f"managed base: {new_parent_head}" in after.stdout
    assert f"last synced parent: {new_parent_head}" in after.stdout


def test_guard_no_rebase_blocks_tracked_branch(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    child = cli(stacker_env, "create", "-b", "child", cwd=repo)
    assert child.returncode == 0, child.stderr
    child_path = Path(child.stdout.strip())

    blocked = cli(stacker_env, "guard", "no-rebase", cwd=child_path)
    assert blocked.returncode == 1
    assert "demo:child is managed by stacker" in blocked.stderr

    allowed = cli(stacker_env, "guard", "no-rebase", cwd=repo)
    assert allowed.returncode == 0, allowed.stderr


def test_sync_conflict_continue(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    child = cli(stacker_env, "create", "-b", "child", cwd=repo)
    child_path = Path(child.stdout.strip())
    commit_file(child_path, stacker_env, "file.txt", "child version\n", "child change")
    commit_file(repo, stacker_env, "file.txt", "parent version\n", "parent change")

    paused = cli(stacker_env, "sync", "child", cwd=repo)
    assert paused.returncode == 0
    assert "Sync paused" in paused.stdout

    (child_path / "file.txt").write_text("resolved\n")
    assert run(["git", "-C", str(child_path), "add", "file.txt"], env=stacker_env).returncode == 0
    resumed = cli(stacker_env, "continue", cwd=repo)
    assert resumed.returncode == 0, resumed.stderr
    assert "Sync complete." in resumed.stdout


def test_sync_conflict_continue_with_repo_outside_repo(repo_factory, stacker_env, tmp_path: Path) -> None:
    repo = repo_factory("demo")
    child = cli(stacker_env, "create", "-b", "child", cwd=repo)
    child_path = Path(child.stdout.strip())
    commit_file(child_path, stacker_env, "file.txt", "child version\n", "child change")
    commit_file(repo, stacker_env, "file.txt", "parent version\n", "parent change")

    paused = cli(stacker_env, "sync", "child", cwd=repo)
    assert paused.returncode == 0
    assert "Sync paused" in paused.stdout

    (child_path / "file.txt").write_text("resolved\n")
    assert run(["git", "-C", str(child_path), "add", "file.txt"], env=stacker_env).returncode == 0
    outside = tmp_path / "outside"
    outside.mkdir()
    resumed = cli(stacker_env, "continue", "--repo", "demo", cwd=outside)
    assert resumed.returncode == 0, resumed.stderr
    assert "Sync complete." in resumed.stdout


def test_sync_continue_skips_empty_cherry_pick_after_manual_continue(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    child = cli(stacker_env, "create", "-b", "child", cwd=repo)
    child_path = Path(child.stdout.strip())
    commit_file(child_path, stacker_env, "file.txt", "child version\n", "child change")
    commit_file(repo, stacker_env, "file.txt", "parent version\n", "parent change")

    paused = cli(stacker_env, "sync", "child", cwd=repo)
    assert paused.returncode == 0
    assert "Sync paused" in paused.stdout

    (child_path / "file.txt").write_text("parent version\n")
    assert run(["git", "-C", str(child_path), "add", "file.txt"], env=stacker_env).returncode == 0
    manual_continue = run(["git", "-C", str(child_path), "cherry-pick", "--continue"], env=stacker_env)
    assert manual_continue.returncode != 0
    assert "previous cherry-pick is now empty" in (manual_continue.stderr + manual_continue.stdout).lower()

    resumed = cli(stacker_env, "continue", cwd=repo)
    assert resumed.returncode == 0, resumed.stderr
    assert "Skipping empty cherry-pick" in resumed.stdout
    assert "Sync complete." in resumed.stdout


def test_sync_continue_after_manual_cherry_pick_continue_does_not_repick(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    child = cli(stacker_env, "create", "-b", "child", cwd=repo)
    child_path = Path(child.stdout.strip())
    commit_file(child_path, stacker_env, "file.txt", "child version\n", "child change")
    commit_file(repo, stacker_env, "file.txt", "parent version\n", "parent change")

    paused = cli(stacker_env, "sync", "child", cwd=repo)
    assert paused.returncode == 0
    assert "Sync paused" in paused.stdout

    (child_path / "file.txt").write_text("resolved custom\n")
    assert run(["git", "-C", str(child_path), "add", "file.txt"], env=stacker_env).returncode == 0
    manual_continue = run(["git", "-C", str(child_path), "cherry-pick", "--continue"], env=stacker_env)
    assert manual_continue.returncode == 0, manual_continue.stderr

    resumed = cli(stacker_env, "continue", cwd=repo)
    assert resumed.returncode == 0, resumed.stderr
    assert "Sync complete." in resumed.stdout
    assert "Cherry-picking" not in resumed.stdout

    count = run(
        ["git", "-C", str(child_path), "rev-list", "--count", "HEAD", "^main"],
        env=stacker_env,
    )
    assert count.stdout.strip() == "1"


def test_sync_conflict_abort(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    child = cli(stacker_env, "create", "-b", "child", cwd=repo)
    child_path = Path(child.stdout.strip())
    commit_file(child_path, stacker_env, "file.txt", "child version\n", "child change")
    original = run(["git", "-C", str(child_path), "rev-parse", "HEAD"], env=stacker_env).stdout.strip()
    commit_file(repo, stacker_env, "file.txt", "parent version\n", "parent change")

    paused = cli(stacker_env, "sync", "child", cwd=repo)
    assert "Sync paused" in paused.stdout
    aborted = cli(stacker_env, "abort", cwd=repo)
    assert aborted.returncode == 0, aborted.stderr
    head = run(["git", "-C", str(child_path), "rev-parse", "HEAD"], env=stacker_env).stdout.strip()
    assert head == original


def test_push_from_untracked_root_resume_and_untracked_files_allowed(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    first = cli(stacker_env, "create", "-b", "child1", cwd=repo)
    child1 = Path(first.stdout.strip())
    second = cli(stacker_env, "create", "-b", "child2", cwd=child1)
    child2 = Path(second.stdout.strip())
    commit_file(child1, stacker_env, "shared.txt", "child1\n", "child1 change")
    commit_file(child2, stacker_env, "shared.txt", "child2\n", "child2 change")
    commit_file(repo, stacker_env, "shared.txt", "root\n", "root change")
    (child1 / "scratch.txt").write_text("untracked is fine\n")

    paused = cli(stacker_env, "push", cwd=repo)
    assert paused.returncode == 0
    assert "Sync paused" in paused.stdout
    assert "child1" in paused.stdout

    (child1 / "shared.txt").write_text("resolved child1\n")
    assert run(["git", "-C", str(child1), "add", "shared.txt"], env=stacker_env).returncode == 0
    resumed = cli(stacker_env, "continue", cwd=repo)
    assert resumed.returncode == 0
    assert "Sync paused" in resumed.stdout
    assert "child2" in resumed.stdout

    (child2 / "shared.txt").write_text("resolved child2\n")
    assert run(["git", "-C", str(child2), "add", "shared.txt"], env=stacker_env).returncode == 0
    finished = cli(stacker_env, "continue", cwd=repo)
    assert finished.returncode == 0, finished.stderr
    assert "Push complete." in finished.stdout


def test_push_preflight_blocks_tracked_changes(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    first = cli(stacker_env, "create", "-b", "child1", cwd=repo)
    child1 = Path(first.stdout.strip())
    (child1 / "file.txt").write_text("dirty\n")

    proc = cli(stacker_env, "push", cwd=repo)
    assert proc.returncode == 1
    assert "Tracked changes present" in proc.stderr


def test_sync_preflight_blocks_staged_tracked_changes(repo_factory, stacker_env) -> None:
    repo = repo_factory("demo")
    child = cli(stacker_env, "create", "-b", "child", cwd=repo)
    assert child.returncode == 0, child.stderr
    child_path = Path(child.stdout.strip())
    commit_file(repo, stacker_env, "file.txt", "parent moved\n", "parent moved")
    (child_path / "file.txt").write_text("staged change\n")
    assert run(["git", "-C", str(child_path), "add", "file.txt"], env=stacker_env).returncode == 0

    proc = cli(stacker_env, "sync", cwd=child_path)
    assert proc.returncode == 1
    assert "Tracked changes present" in proc.stderr
