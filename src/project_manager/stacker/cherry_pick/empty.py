from __future__ import annotations

from project_manager.stacker.models import OperationState


def is_empty_cherry_pick_message(message: str) -> bool:
    return "previous cherry-pick is now empty" in message.lower()


def should_skip_empty_commit(op: OperationState) -> bool:
    return (
        op.next_commit_index < len(op.commit_list)
        and bool(op.error_message)
        and is_empty_cherry_pick_message(op.error_message)
    )
