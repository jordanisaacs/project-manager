from project_manager.paths import Paths


def list_repos(paths: Paths) -> list[str]:
    """Sorted names of direct children of paths.repos that contain a .git entry."""
    if not paths.repos.is_dir():
        return []
    return sorted(
        entry.name
        for entry in paths.repos.iterdir()
        if entry.is_dir() and (entry / ".git").exists()
    )
