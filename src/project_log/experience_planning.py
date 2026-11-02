"""Lossless local subdivision; fitting requests and their order remain unchanged."""

from collections.abc import Callable, Sequence

PLANNING_POLICY = "full-wire-split-all-evidence-priority-v1"


def subdivide[T](
    items: Sequence[T],
    fits: Callable[[Sequence[T]], bool],
    split_one: Callable[[T], Sequence[T]] | None = None,
) -> list[tuple[T, ...]]:
    if fits(items):
        return [tuple(items)]
    if len(items) > 1:
        middle = len(items) // 2
        return subdivide(items[:middle], fits, split_one) + subdivide(
            items[middle:], fits, split_one
        )
    if len(items) == 1 and split_one:
        children = split_one(items[0])
        if len(children) > 1:
            return [part for child in children for part in subdivide((child,), fits, split_one)]
    # The caller records/isolates the exact unsplittable unit. Nothing is truncated,
    # approved, silently omitted, sent, or given an additional attempt here.
    return [tuple(items)]
