from pathlib import Path

import pytest

from project_manager.agent import run as run_mod
from project_manager.agent.run import AgentName
from project_manager.errors import ProjectError
from project_manager.paths import Paths
from project_manager.project import create as create_mod
from tests.helpers import git_pool


def _just(wts: list[str]) -> list[tuple[str, str]]:
    return [(w, w) for w in wts]


# --- AgentName ---


def test_agent_name_members_are_exhaustive() -> None:
    """Locks the set so adding a fourth needs a conscious update here
    and a matching cyclopts stub in agent/cli/run.py."""
    assert {a.value for a in AgentName} == {"claude", "codex", "cursor"}


# --- resume_args ---


def test_resume_args_claude() -> None:
    assert run_mod.resume_args(AgentName.CLAUDE, "abc") == ("--resume", "abc")


def test_resume_args_codex_uses_subcommand_form() -> None:
    # Codex takes `resume <id>` as a subcommand (no `--resume` flag).
    assert run_mod.resume_args(AgentName.CODEX, "xyz") == ("resume", "xyz")


def test_resume_args_cursor() -> None:
    assert run_mod.resume_args(AgentName.CURSOR, "uvw") == ("--resume", "uvw")


# --- build_command ---


def test_build_command_claude_appends_forwarded_args() -> None:
    cwd, argv = run_mod.build_command(
        AgentName.CLAUDE,
        Path("/proj/a"),
        ("--resume", "xxx"),
        {"claude": "isaac", "codex": "isaac codex --", "cursor": "agent"},
    )
    assert cwd == Path("/proj/a")
    assert argv == ["isaac", "--resume", "xxx"]


def test_build_command_codex_preserves_double_dash_separator() -> None:
    """Codex's wrapper ends in `--`; the user's forwarded args need to
    land AFTER that separator so codex receives them as its own argv."""
    _cwd, argv = run_mod.build_command(
        AgentName.CODEX,
        Path("/proj/a"),
        ("write me a script",),
        {"codex": "isaac codex --"},
    )
    assert argv == ["isaac", "codex", "--", "write me a script"]


def test_build_command_cursor_no_forwarded_args() -> None:
    _cwd, argv = run_mod.build_command(
        AgentName.CURSOR,
        Path("/proj/a"),
        (),
        {"cursor": "agent"},
    )
    assert argv == ["agent"]


def test_build_command_missing_config_raises() -> None:
    with pytest.raises(ProjectError, match=r"no \[agents\.commands\]\.codex"):
        run_mod.build_command(
            AgentName.CODEX,
            Path("/proj/a"),
            (),
            {"claude": "isaac"},
        )


def test_build_command_empty_string_raises() -> None:
    with pytest.raises(ProjectError, match="is empty"):
        run_mod.build_command(
            AgentName.CLAUDE,
            Path("/proj/a"),
            (),
            {"claude": "   "},
        )


# --- run() — dispatch path with monkeypatched exec ---


def _prepare_project(pm_env: Paths) -> Path:
    git_pool(pm_env, "foo", n=1)
    create_mod.create(pm_env, "demo", _just(["foo"]))
    return pm_env.project("demo")


def _write_config_with_agents(
    pm_env: Paths,
    commands: dict[str, str],
) -> None:
    """The pm_env fixture already wrote a `[paths]`-only config and set
    PM_CONFIG. Append an `[agents.commands]` table by rewriting the file
    with the same paths plus the supplied agent commands.
    """
    import os

    cfg_path = Path(os.environ["PM_CONFIG"])
    body = (
        "[paths]\n"
        f'repos = "{pm_env.repos}"\n'
        f'worktrees = "{pm_env.worktrees}"\n'
        f'projects = "{pm_env.projects}"\n'
        f'stacker_root = "{pm_env.stacker_root}"\n'
        "[agents.commands]\n"
    )
    for name, cmd in commands.items():
        body += f'{name} = "{cmd}"\n'
    cfg_path.write_text(body)


def test_run_execvps_the_configured_command_in_project_dir(
    pm_env: Paths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = _prepare_project(pm_env)
    _write_config_with_agents(pm_env, {"claude": "isaac"})

    recorded: dict[str, object] = {}

    def fake_chdir(p: str | Path) -> None:
        recorded["cwd"] = Path(p)

    def fake_execvp(file: str, argv: list[str]) -> None:
        recorded["file"] = file
        recorded["argv"] = list(argv)
        # Raise SystemExit so `run()` (annotated NoReturn) never
        # returns — matches real execvp semantics in the test.
        raise SystemExit(0)

    monkeypatch.setattr(run_mod.os, "chdir", fake_chdir)
    monkeypatch.setattr(run_mod.os, "execvp", fake_execvp)
    # detect_current_project consults $PWD before os.getcwd(), so
    # monkeypatch.chdir alone isn't enough — sync PWD too.
    monkeypatch.chdir(project_dir)
    monkeypatch.setenv("PWD", str(project_dir))

    with pytest.raises(SystemExit):
        run_mod.run(AgentName.CLAUDE, None, ("--resume", "abc"))

    assert recorded["cwd"] == project_dir
    assert recorded["file"] == "isaac"
    assert recorded["argv"] == ["isaac", "--resume", "abc"]


def test_run_rewraps_filenotfound_as_project_error(
    pm_env: Paths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_project(pm_env)
    _write_config_with_agents(pm_env, {"claude": "definitely-not-on-path"})

    def fake_execvp(file: str, _argv: list[str]) -> None:
        raise FileNotFoundError(2, "No such file", file)

    monkeypatch.setattr(run_mod.os, "chdir", lambda _p: None)
    monkeypatch.setattr(run_mod.os, "execvp", fake_execvp)
    project_dir = pm_env.project("demo")
    monkeypatch.chdir(project_dir)
    monkeypatch.setenv("PWD", str(project_dir))

    with pytest.raises(ProjectError, match="agent command not found"):
        run_mod.run(AgentName.CLAUDE, None, ())


def test_run_raises_when_cwd_not_in_any_project(
    pm_env: Paths,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Mirrors the `-p` requirement when the user is outside any pm
    project — uses the existing `resolve_project` behavior, so we
    just need to confirm the error surfaces."""
    _write_config_with_agents(pm_env, {"claude": "isaac"})
    # Move cwd to somewhere unrelated to any pm project.
    unrelated = tmp_path / "elsewhere"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    monkeypatch.setenv("PWD", str(unrelated))
    with pytest.raises(ProjectError, match="no project specified"):
        run_mod.run(AgentName.CLAUDE, None, ())
