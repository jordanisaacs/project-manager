class CommandError(RuntimeError):
    """A subprocess command failed."""


class ProjectError(Exception):
    """A project-level operation failed."""
