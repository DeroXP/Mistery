"""Shuffle that sounds random, instead of shuffle that is random.

A uniformly random order is exactly what a shuffle button promises and exactly
what people then call broken: it happily plays three songs from one album back
to back, because true randomness clusters. What listeners mean by "shuffle" is
*spread out*.

So tracks are grouped (by album, which also spreads artists who have several
albums) and each group's tracks are placed at evenly spaced points along the
queue — with a random starting offset per group and a little jitter per track,
so the result is different every time but never bunched.
"""

from __future__ import annotations

import random
from typing import Callable, Sequence, TypeVar

T = TypeVar("T")


def smart_shuffle(items: Sequence[T], group_of: Callable[[T], object],
                  rng: random.Random | None = None) -> list[T]:
    rng = rng or random.Random()
    groups: dict[object, list[T]] = {}
    for item in items:
        groups.setdefault(group_of(item), []).append(item)

    placed: list[tuple[float, float, T]] = []
    for members in groups.values():
        members = list(members)
        rng.shuffle(members)
        count = len(members)
        spacing = 1.0 / count
        offset = rng.uniform(0, spacing)
        for index, member in enumerate(members):
            jitter = rng.uniform(-0.2, 0.2) * spacing
            # The second key breaks exact ties randomly rather than by group order.
            placed.append((offset + index * spacing + jitter, rng.random(), member))
    placed.sort(key=lambda entry: (entry[0], entry[1]))
    return [member for _, _, member in placed]


def worst_run(order: Sequence[T], group_of: Callable[[T], object]) -> int:
    """Longest stretch of consecutive items from one group — lower is better."""
    longest = current = 0
    previous = object()
    for item in order:
        group = group_of(item)
        current = current + 1 if group == previous else 1
        longest = max(longest, current)
        previous = group
    return longest
