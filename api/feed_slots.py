"""Pure slot-math for the SwipeUp feed engine.

No DB access, no FastAPI, no NumPy — easy to unit test and reason about.

Concepts:
  slot_mix      — how many slots out of `batch_size` are inspiration vs niche
  niche_allocation — within the niche slots, how many per niche
                     (round-robin remainder)
  slot_sequence — the interleaved per-position assignment, e.g.
                  ['dogs', 'INSPIRATION', 'cats', 'horses', 'INSPIRATION', ...]
  creator_penalty — multiplicative factor applied to a candidate's score
                    during slot assignment when its creator has appeared
                    recently or many times this session

These functions are called in this order from build_batch():

  1. count_unseen_inspiration  (in feed_pools, DB)
  2. compute_slot_mix          (here)
  3. generate_interleaved_sequence (here)
  4. retrieve_and_rerank pools (in feed_pools, DB)
  5. for each slot: pick best candidate * creator_penalty (here)
"""
from __future__ import annotations

from typing import Iterable

# ── Tunable knobs ─────────────────────────────────────────────────────────────

# Max share of a batch that can come from Tracked creators. The actual share
# is `min(unseen_inspiration_count, batch_size * ratio)` — if supply is short,
# we just have fewer inspiration slots.
INSPIRATION_TARGET_RATIO = 0.60

# Floor and recovery curve for the back-to-back creator penalty.
COOLDOWN_BASE = 0.3                # score multiplier immediately after a creator was used
COOLDOWN_RECOVERY_SLOTS = 6        # slots until the penalty is fully restored to 1.0

# Cumulative penalty for a creator that's already appeared N times this session.
COOLDOWN_CUMULATIVE: dict[int, float] = {0: 1.0, 1: 0.7, 2: 0.5, 3: 0.3}
COOLDOWN_CUMULATIVE_FLOOR = 0.2


# ── Slot mix ──────────────────────────────────────────────────────────────────

def compute_slot_mix(
    unseen_inspiration_count: int,
    account_niches: list[tuple[str, float]],
    batch_size: int = 15,
) -> tuple[int, dict[str, int]]:
    """Reserve up to INSPIRATION_TARGET_RATIO of the batch for inspiration,
    capped by actual unseen supply. Remaining slots split evenly across the
    user's selected niches (with weight order breaking the remainder tie).

    `account_niches` is the [(niche_id, weight), ...] list already ordered
    DESC by weight — `allocate_niche_slots` uses that order for the remainder.

    Returns (inspiration_slots, {niche_id: slot_count}).
    """
    inspiration_max   = int(batch_size * INSPIRATION_TARGET_RATIO)
    inspiration_slots = min(max(unseen_inspiration_count, 0), inspiration_max)
    niche_slots       = batch_size - inspiration_slots
    niche_allocation  = allocate_niche_slots(account_niches, niche_slots)
    return inspiration_slots, niche_allocation


def allocate_niche_slots(
    account_niches: list[tuple[str, float]],
    total_niche_slots: int,
) -> dict[str, int]:
    """Round-robin distribution. With 13 slots across 4 niches: 4/3/3/3.
    The remainder is taken by the highest-weight niches first."""
    n = len(account_niches)
    if n == 0 or total_niche_slots == 0:
        return {}
    base      = total_niche_slots // n
    remainder = total_niche_slots % n
    return {
        niche_id: base + (1 if i < remainder else 0)
        for i, (niche_id, _w) in enumerate(account_niches)
    }


# ── Slot sequence ─────────────────────────────────────────────────────────────

INSPIRATION_KEY = 'INSPIRATION'


def generate_interleaved_sequence(
    niche_allocation: dict[str, int],
    inspiration_slots: int,
) -> list[str]:
    """Produce the per-position slot key sequence.

    Rules:
      - Niche slots round-robin in count-DESC order so the heaviest niche
        appears most frequently and is never bunched (dogs, cats, dogs, cats,
        not dogs, dogs, cats, cats).
      - INSPIRATION slots distributed evenly across positions via the
        round(i * total / (k+1)) trick — that gives positions roughly
        equispaced and avoids clustering at the ends.
      - When niche_slots > 0 the first and last positions tend to be niche
        because the inspiration positions land at fractional indices that
        skip 0 and total-1.
      - Edge cases:
          * zero inspiration  → pure round-robin niche stream
          * zero niche        → pure inspiration stream
          * single niche      → the niche stream is just that niche repeated
    """
    # Build the niche stream by round-robin over counts.
    niche_stream: list[str] = []
    counts = dict(niche_allocation)
    niches_sorted = sorted(niche_allocation.keys(), key=lambda n: -niche_allocation[n])
    while any(counts.values()):
        for n in niches_sorted:
            if counts[n] > 0:
                niche_stream.append(n)
                counts[n] -= 1

    if inspiration_slots == 0:
        return niche_stream
    if not niche_stream:
        return [INSPIRATION_KEY] * inspiration_slots

    total = len(niche_stream) + inspiration_slots
    inspiration_positions = {
        round(i * total / (inspiration_slots + 1))
        for i in range(1, inspiration_slots + 1)
    }

    sequence: list[str] = []
    niche_idx       = 0
    inspiration_idx = 0
    for pos in range(total):
        if pos in inspiration_positions and inspiration_idx < inspiration_slots:
            sequence.append(INSPIRATION_KEY)
            inspiration_idx += 1
        elif niche_idx < len(niche_stream):
            sequence.append(niche_stream[niche_idx])
            niche_idx += 1
        else:
            # Defensive — should only hit if `inspiration_positions` collides
            # with itself (round() to same int). Fill with inspiration so the
            # batch length stays consistent.
            sequence.append(INSPIRATION_KEY)
            inspiration_idx += 1
    return sequence


# ── Creator cooldown ──────────────────────────────────────────────────────────

def creator_penalty(
    creator_id: str,
    batch_creators: list[tuple[str, int]],
    session_creator_counts: dict[str, int],
    current_slot_pos: int,
) -> float:
    """Multiplier applied to a candidate's score during slot assignment.

    Two effects compose:
      * `cumulative`: based on how many times this creator has been shown
        in the *whole session* (per session_creator_counts).
      * `recency`:    based on how many slots ago this creator last appeared
        in *this batch* (per batch_creators).

    Returned penalty is cumulative * recency. 1.0 means no penalty.
    """
    times_shown = session_creator_counts.get(creator_id, 0)
    cumulative  = COOLDOWN_CUMULATIVE.get(times_shown, COOLDOWN_CUMULATIVE_FLOOR)

    recent = [pos for cid, pos in batch_creators if cid == creator_id]
    if not recent:
        recency = 1.0
    else:
        slots_since = current_slot_pos - max(recent)
        recency = min(
            1.0,
            COOLDOWN_BASE + (slots_since / COOLDOWN_RECOVERY_SLOTS) * (1.0 - COOLDOWN_BASE),
        )

    return cumulative * recency


__all__ = [
    'INSPIRATION_TARGET_RATIO',
    'COOLDOWN_BASE',
    'COOLDOWN_RECOVERY_SLOTS',
    'COOLDOWN_CUMULATIVE',
    'COOLDOWN_CUMULATIVE_FLOOR',
    'INSPIRATION_KEY',
    'compute_slot_mix',
    'allocate_niche_slots',
    'generate_interleaved_sequence',
    'creator_penalty',
]
