"""`pm agent serve`: a headless agent-session tracking daemon.

It ingests best-effort lifecycle-hook events over a loopback HTTP
endpoint, keeps live session state in an ephemeral WAL SQLite store
(lifetime = the serve process), backfills via a transcript-tailing
fallback, and pushes identifier-rich updates over SSE. It never drives an
agent process; lifecycle events such as Codex `/clear` only clean up the
daemon's own live-session rows.
"""
