"""Thin dispatcher: delegates argparse wiring to stacker/commands/*.

gc_ops is re-exported here for back-compat with tests and external callers
that import from project_manager.stacker.cli.
"""
from __future__ import annotations

from .commands import add_subparser
from .gc import gc_ops

__all__ = ["add_subparser", "gc_ops"]
