from __future__ import annotations

import os
import stat
import subprocess
import sys
import json
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        input=input_text,
        capture_output=True,
        check=False,
    )
    return proc


@pytest.fixture
def stacker_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    scan_root = tmp_path / "repos"
    scan_root.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    _write_repo_scan(bin_dir / "repo-scan")
    _write_wt(bin_dir / "wt")
    _write_gh(bin_dir / "gh")

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["XDG_STATE_HOME"] = str(state)
    env["STACKER_SCAN_ROOT"] = str(scan_root)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["GIT_AUTHOR_NAME"] = "Stacker Test"
    env["GIT_AUTHOR_EMAIL"] = "stacker@example.com"
    env["GIT_COMMITTER_NAME"] = "Stacker Test"
    env["GIT_COMMITTER_EMAIL"] = "stacker@example.com"
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("XDG_STATE_HOME", env["XDG_STATE_HOME"])
    monkeypatch.setenv("STACKER_SCAN_ROOT", env["STACKER_SCAN_ROOT"])
    monkeypatch.setenv("PYTHONPATH", env["PYTHONPATH"])
    monkeypatch.setenv("PATH", env["PATH"])
    return env


@pytest.fixture
def repo_factory(stacker_env: dict[str, str]):
    scan_root = Path(stacker_env["STACKER_SCAN_ROOT"])

    def create_repo(name: str) -> Path:
        repo = scan_root / name
        repo.mkdir()
        assert run(["git", "init", "-b", "main"], cwd=repo, env=stacker_env).returncode == 0
        (repo / "file.txt").write_text("root\n")
        assert run(["git", "add", "file.txt"], cwd=repo, env=stacker_env).returncode == 0
        assert run(["git", "commit", "-m", "initial"], cwd=repo, env=stacker_env).returncode == 0
        return repo

    return create_repo


def cli(env: dict[str, str], *args: str, cwd: Path | None = None, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return run([sys.executable, "-m", "stacker", *args], cwd=cwd, env=env, input_text=input_text)


def commit_file(repo: Path, env: dict[str, str], relpath: str, content: str, message: str) -> None:
    path = repo / relpath
    path.write_text(content)
    assert run(["git", "add", relpath], cwd=repo, env=env).returncode == 0
    assert run(["git", "commit", "-m", message], cwd=repo, env=env).returncode == 0


def configure_gh_repo(repo: Path, env: dict[str, str], slug: str) -> None:
    assert run(["git", "-C", str(repo), "config", "stacker.gh-repo", slug], env=env).returncode == 0


def read_fake_prs(env: dict[str, str]) -> list[dict]:
    store = Path(env["XDG_STATE_HOME"]) / "gh-prs.json"
    if not store.exists():
        return []
    return json.loads(store.read_text())


def _write_repo_scan(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import os
import subprocess
import sys
from pathlib import Path

root = Path(os.environ["STACKER_SCAN_ROOT"])
repos = []
for child in sorted(root.iterdir()):
    if (child / ".git").exists():
        repos.append((child.name, str(child)))

if len(sys.argv) > 1 and sys.argv[1] == "--complete":
    prefix = sys.argv[3] if len(sys.argv) > 3 else ""
    worktrees_base = Path(os.environ["HOME"]) / ".worktrees"
    out = []
    for name, repo in repos:
        repo_entry = f"{name}:"
        if repo_entry.startswith(prefix) or prefix in repo_entry:
            out.append(repo_entry)
        wt_dir = worktrees_base / Path(repo).name
        if wt_dir.exists():
            for child in sorted(wt_dir.iterdir()):
                entry = f"{name}:{child.name}"
                if entry.startswith(prefix) or prefix in entry:
                    out.append(entry)
    print("\\n".join(out))
else:
    for name, repo in repos:
        print(f"{name}|{repo}")
"""
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _write_wt(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import os
import subprocess
import sys
from pathlib import Path

home = Path(os.environ["HOME"])
scan_root = Path(os.environ["STACKER_SCAN_ROOT"])
worktrees_base = home / ".worktrees"

def find_repo(name: str) -> Path:
    for child in scan_root.iterdir():
        if child.name == name:
            return child
    raise SystemExit(f"unknown repo: {name}")

def in_repo(path: Path) -> bool:
    proc = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], text=True, capture_output=True)
    return proc.returncode == 0

def current_repo(path: Path) -> Path:
    proc = subprocess.run(["git", "-C", str(path), "rev-parse", "--git-common-dir"], text=True, capture_output=True, check=True)
    common = proc.stdout.strip()
    if common.endswith("/.git"):
        return Path(common).parent
    return (path / common).resolve().parent

def branch_exists(repo: Path, branch: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", branch], text=True, capture_output=True).returncode == 0

args = sys.argv[1:]
if not args:
    raise SystemExit(1)
command = args.pop(0)
if command not in ("create", "delete"):
    raise SystemExit("unsupported")

if command == "delete":
    delete_branch = False
    while args and args[0] in ("-b", "--delete-branch"):
        delete_branch = True
        args.pop(0)
    target = args[0] if args else None
    if not target:
        raise SystemExit("missing target")
    if ":" in target:
        repo_name, branch = target.split(":", 1)
        repo = find_repo(repo_name)
    else:
        repo = current_repo(Path.cwd())
        branch = target
    wt_path = worktrees_base / repo.name / branch
    proc = subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt_path)], text=True)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)
    if delete_branch:
        proc = subprocess.run(["git", "-C", str(repo), "branch", "-D", branch], text=True)
        if proc.returncode != 0:
            raise SystemExit(proc.returncode)
    raise SystemExit(0)

create_branch = False
base_branch = None
result_file = None
target = None
while args:
    arg = args.pop(0)
    if arg == "-b":
        create_branch = True
    elif arg in ("-B", "--base"):
        base_branch = args.pop(0)
        create_branch = True
    elif arg == "--result-file":
        result_file = args.pop(0)
    else:
        target = arg
if not target:
    raise SystemExit("missing target")

if ":" in target:
    repo_name, branch = target.split(":", 1)
    repo = find_repo(repo_name)
else:
    repo = current_repo(Path.cwd())
    branch = target

if not branch_exists(repo, branch) and not create_branch:
    sys.stdout.write(f"Branch '{branch}' does not exist.\\nCreate new branch '{branch}'? [y/N] ")
    sys.stdout.flush()
    response = sys.stdin.readline().strip().lower()
    if response != "y":
        raise SystemExit(1)
    create_branch = True

wt_path = worktrees_base / repo.name / branch
wt_path.parent.mkdir(parents=True, exist_ok=True)
cmd = ["git", "-C", str(repo), "worktree", "add", str(wt_path)]
if create_branch:
    cmd.extend(["-b", branch])
    if base_branch:
        cmd.append(base_branch)
else:
    cmd.append(branch)
proc = subprocess.run(cmd, text=True)
if proc.returncode != 0:
    raise SystemExit(proc.returncode)
if result_file:
    Path(result_file).write_text(str(wt_path))
else:
    print(wt_path)
"""
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _write_gh(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import subprocess
import sys
from pathlib import Path

state_path = Path(__import__("os").environ["XDG_STATE_HOME"]) / "gh-prs.json"
if state_path.exists():
    prs = json.loads(state_path.read_text())
else:
    prs = []

def save():
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(prs))

def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)

def current_repo_slug(cwd):
    out = git("-C", cwd, "config", "stacker.gh-repo").stdout.strip()
    if not out:
        raise SystemExit("missing stacker.gh-repo config")
    return out

args = sys.argv[1:]
if not args:
    raise SystemExit(1)
if args[:2] == ["repo", "view"]:
    repo = None
    if "--repo" in args:
        repo = args[args.index("--repo") + 1]
    else:
        repo = current_repo_slug(Path.cwd())
    owner, name = repo.split("/", 1)
    print(json.dumps({"name": name, "owner": {"login": owner}}))
    raise SystemExit(0)

if args[:2] == ["pr", "list"]:
    repo = args[args.index("--repo") + 1]
    head = args[args.index("--head") + 1] if "--head" in args else None
    search = args[args.index("--search") + 1] if "--search" in args else None
    out = []
    for pr in prs:
        if pr["repo"] != repo or pr["state"] != "OPEN":
            continue
        if head and pr["headRefName"] != head:
            continue
        if search:
            if search.startswith("is:open head:"):
                target = search[len("is:open head:"):]
                if pr["headSearch"] != target:
                    continue
        out.append({k: pr[k] for k in ("number", "url", "title", "body", "headRefName", "baseRefName", "state", "isDraft")})
    print(json.dumps(out))
    raise SystemExit(0)

if args[:2] == ["pr", "create"]:
    repo = args[args.index("--repo") + 1]
    base = args[args.index("--base") + 1]
    head = args[args.index("--head") + 1]
    title = args[args.index("--title") + 1]
    body_file = args[args.index("--body-file") + 1]
    body = Path(body_file).read_text()
    number = max([pr["number"] for pr in prs], default=0) + 1
    if ":" in head:
        owner, head_ref = head.split(":", 1)
        head_search = head
    else:
        owner = repo.split("/", 1)[0]
        head_ref = head
        head_search = f"{owner}:{head_ref}"
    pr = {
        "number": number,
        "url": f"https://github.com/{repo}/pull/{number}",
        "title": title,
        "body": body,
        "headRefName": head_ref,
        "baseRefName": base,
        "state": "OPEN",
        "isDraft": "--draft" in args,
        "repo": repo,
        "headSearch": head_search,
    }
    prs.append(pr)
    save()
    print(pr["url"])
    raise SystemExit(0)

if args[:2] == ["pr", "edit"]:
    number = int(args[2])
    repo = args[args.index("--repo") + 1]
    pr = next((item for item in prs if item["repo"] == repo and item["number"] == number), None)
    if pr is None:
        raise SystemExit("missing pr")
    if "--title" in args:
        pr["title"] = args[args.index("--title") + 1]
    if "--body-file" in args:
        pr["body"] = Path(args[args.index("--body-file") + 1]).read_text()
    if "--base" in args:
        pr["baseRefName"] = args[args.index("--base") + 1]
    save()
    raise SystemExit(0)

raise SystemExit(f"unsupported gh invocation: {' '.join(args)}")
"""
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
