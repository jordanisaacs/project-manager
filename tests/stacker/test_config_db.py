from __future__ import annotations

import pytest

from project_manager.paths import Paths
from project_manager.stacker.db import StackerDB


@pytest.fixture
def db(pm_env: Paths) -> StackerDB:
    return StackerDB(pm_env.stacker_db())


def test_get_unset_returns_none(db: StackerDB) -> None:
    assert db.get_config("demo", "pr.mode") is None


def test_set_then_get_roundtrip(db: StackerDB) -> None:
    db.set_config("demo", "pr.mode", "repo-pr")
    assert db.get_config("demo", "pr.mode") == "repo-pr"


def test_set_overwrites_existing(db: StackerDB) -> None:
    db.set_config("demo", "pr.mode", "pr-pr")
    db.set_config("demo", "pr.mode", "repo-pr")
    assert db.get_config("demo", "pr.mode") == "repo-pr"


def test_unset_removes_key_and_reports_prior_presence(db: StackerDB) -> None:
    db.set_config("demo", "pr.trunk", "master")
    assert db.unset_config("demo", "pr.trunk") is True
    assert db.get_config("demo", "pr.trunk") is None
    assert db.unset_config("demo", "pr.trunk") is False


def test_list_is_scoped_to_repo_and_sorted(db: StackerDB) -> None:
    db.set_config("demo", "pr.trunk", "master")
    db.set_config("demo", "pr.mode", "repo-pr")
    db.set_config("other", "pr.mode", "pr-pr")
    assert db.list_config("demo") == [
        ("pr.mode", "repo-pr"),
        ("pr.trunk", "master"),
    ]
    assert db.list_config("other") == [("pr.mode", "pr-pr")]
