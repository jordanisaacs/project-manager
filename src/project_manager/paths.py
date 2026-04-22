from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    repos: Path
    worktrees: Path
    projects: Path
    stacker_root: Path

    def repo(self, name: str) -> Path:
        return self.repos / name

    def pool(self, repo: str) -> Path:
        return self.worktrees / repo

    def slot(self, repo: str, uuid: str) -> Path:
        return self.worktrees / repo / uuid

    def owner(self, repo: str, uuid: str) -> Path:
        return self.worktrees / repo / uuid / ".owner"

    def project(self, name: str) -> Path:
        return self.projects / name

    def forward(self, project: str, repo: str) -> Path:
        return self.projects / project / repo

    def project_db(self, project: str) -> Path:
        return self.projects / project / ".pm.db"

    def stacker_db(self) -> Path:
        return self.stacker_root / "db.sqlite"

    def stacker_ops_marker(self) -> Path:
        return self.stacker_root / "ops"
