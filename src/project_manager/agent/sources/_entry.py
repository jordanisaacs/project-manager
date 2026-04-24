"""Shared `SessionEntry` dataclass.

Split out of `sources/__init__.py` so each source adapter can depend on
the entry type without creating a circular import through the package
`__init__`.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class SessionEntry:
    """One chat/agent session, normalized across vendors.

    `session_id` is whatever the vendor's resume command expects (full
    UUID for all three today). `cwd` is informational — scoping is
    already done by the time an entry is built.
    """

    agent: str
    session_id: str
    title: str
    last_active: datetime
    cwd: Path
