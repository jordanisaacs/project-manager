import asyncio

import pytest

from project_manager.async_util import bounded_gather, bounded_gather_map


def test_bounded_gather_preserves_input_order() -> None:
    async def leaf(i: int) -> int:
        # Sleep inversely to i so later tasks finish first — if the helper
        # used completion order the result list would be reversed.
        await asyncio.sleep((10 - i) * 0.001)
        return i

    result = asyncio.run(
        bounded_gather((leaf(i) for i in range(10)), limit=5),
    )
    assert result == list(range(10))


def test_bounded_gather_respects_limit() -> None:
    in_flight = 0
    peak = 0

    async def leaf() -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        # Yield to the loop so other tasks can try to enter; if the
        # semaphore weren't holding, they would and peak would climb.
        await asyncio.sleep(0.005)
        in_flight -= 1

    asyncio.run(bounded_gather((leaf() for _ in range(20)), limit=3))
    assert peak == 3


def test_bounded_gather_map_returns_dict_keyed_on_inputs() -> None:
    async def double(i: int) -> int:
        return i * 2

    result = asyncio.run(
        bounded_gather_map(range(5), double, limit=2),
    )
    assert result == {0: 0, 1: 2, 2: 4, 3: 6, 4: 8}


def test_bounded_gather_rejects_zero_limit() -> None:
    # Pass an empty list — the validation must run before we'd iterate
    # awaitables anyway, so creating a real coroutine here would only
    # leak it.
    with pytest.raises(ValueError, match=r"limit must be >= 1"):
        asyncio.run(bounded_gather([], limit=0))


def test_bounded_gather_surfaces_child_exceptions() -> None:
    async def boom() -> None:
        raise RuntimeError("nope")

    async def ok() -> int:
        await asyncio.sleep(0.001)
        return 1

    with pytest.raises(ExceptionGroup) as excinfo:
        asyncio.run(bounded_gather([ok(), boom(), ok()], limit=2))
    # At least one RuntimeError inside the group — TaskGroup may attach
    # CancelledError siblings from the in-flight tasks alongside it.
    assert any(isinstance(e, RuntimeError) for e in excinfo.value.exceptions)
