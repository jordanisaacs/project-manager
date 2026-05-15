"""Small async utilities shared by concurrent fanouts.

`bounded_gather` is the one-stop replacement for the
`asyncio.Semaphore + asyncio.TaskGroup` pattern that otherwise gets
duplicated every time a subsystem wants a bounded fanout — see
`repo.maintenance._maintain_async` and `stacker.render.prefetch.prefetch_all`.
Both the structured concurrency (any child raising cancels siblings and
surfaces as `ExceptionGroup`) and the input-order-preserving result list
are guarantees the callers depend on.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import TypeVar

T = TypeVar("T")
K = TypeVar("K")


async def bounded_gather(
    awaitables: Iterable[Awaitable[T]],
    *,
    limit: int,
) -> list[T]:
    """Run awaitables concurrently with at most `limit` in flight.

    Wraps each coroutine in a semaphore-acquiring shim and launches them in
    a `TaskGroup`, so a failure cancels siblings and surfaces as
    `ExceptionGroup`. Results come back in the same order as `awaitables`.

    `limit <= 0` raises `ValueError` — unbounded callers don't need this
    helper; they can use `asyncio.gather` directly. Forcing callers to
    pass a positive cap keeps "I forgot to set a limit" from being a
    silent foot-gun.
    """
    if limit <= 0:
        raise ValueError(f"limit must be >= 1, got {limit}")
    sem = asyncio.Semaphore(limit)
    coros = list(awaitables)

    async def _bounded(coro: Awaitable[T]) -> T:
        async with sem:
            return await coro

    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(_bounded(c)) for c in coros]
    return [t.result() for t in tasks]


async def bounded_gather_map(
    items: Iterable[K],
    fn: Callable[[K], Awaitable[T]],
    *,
    limit: int,
) -> dict[K, T]:
    """`bounded_gather` convenience keyed on the input item.

    Same semantics as `bounded_gather`, except the result is `{item:
    fn(item)}`. Useful when the caller wants to look results up later and
    would otherwise zip the input list against a positional result list.
    Duplicate items in the input collapse into one call — caller beware
    if that matters.
    """
    item_list = list(items)
    results = await bounded_gather(
        (fn(item) for item in item_list),
        limit=limit,
    )
    return dict(zip(item_list, results, strict=True))
