"""`pm agent serve`: a headless agent-session tracking daemon.

A pure observer — it ingests best-effort lifecycle-hook events over a
loopback HTTP endpoint, keeps live session state in an ephemeral WAL
SQLite store (lifetime = the serve process), backfills via a transcript-
tailing fallback, and pushes identifier-rich updates over SSE. It never
acts on a session; every emitted event carries the `agent` type and
`vendor_session_id` so observers (e.g. an Emacs client) decide what to do.
"""
