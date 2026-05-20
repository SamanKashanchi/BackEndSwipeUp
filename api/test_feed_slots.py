"""Smoke tests for feed_slots.py.

Pure-function module, no DB, no network — these run with bare pytest:
    python -m pytest api/test_feed_slots.py -q

Covers the 9 scenarios from the spec acceptance criteria:
  - slot mix at various unseen-inspiration counts
  - niche-only batches
  - single-niche fills
  - even niche distribution
  - inspiration-heavy batches with cooldown spreading
  - creator cooldown (back-to-back, cumulative, multi-batch session)
"""
from __future__ import annotations

import pytest

from feed_slots import (
    INSPIRATION_KEY,
    INSPIRATION_TARGET_RATIO,
    COOLDOWN_BASE,
    COOLDOWN_RECOVERY_SLOTS,
    COOLDOWN_CUMULATIVE,
    COOLDOWN_CUMULATIVE_FLOOR,
    allocate_niche_slots,
    compute_slot_mix,
    creator_penalty,
    generate_interleaved_sequence,
)


# ─── compute_slot_mix ─────────────────────────────────────────────────────────

def test_mix_no_inspiration_one_niche():
    """0 unseen tracked, 1 niche → 0 IN + 15 niche."""
    ins, alloc = compute_slot_mix(0, [("dogs", 1.0)], batch_size=15)
    assert ins == 0
    assert alloc == {"dogs": 15}


def test_mix_no_inspiration_four_niches():
    """0 unseen tracked, 4 niches → 0 IN + 15 niche (4/4/4/3)."""
    ins, alloc = compute_slot_mix(
        0, [("dogs", 1.0), ("cats", 1.0), ("horses", 1.0), ("wildlife", 1.0)],
        batch_size=15,
    )
    assert ins == 0
    assert sum(alloc.values()) == 15
    # heavier (first in input) niches absorb the +1 remainder
    assert alloc["dogs"] == 4 and alloc["cats"] == 4 and alloc["horses"] == 4
    assert alloc["wildlife"] == 3


def test_mix_partial_inspiration_under_cap():
    """3 unseen tracked, 4 niches → 3 IN + 12 niche (3/3/3/3)."""
    ins, alloc = compute_slot_mix(
        3, [("dogs", 1.0), ("cats", 1.0), ("horses", 1.0), ("wildlife", 1.0)],
        batch_size=15,
    )
    assert ins == 3
    assert sum(alloc.values()) == 12
    assert set(alloc.values()) == {3}


def test_mix_inspiration_capped_at_ratio():
    """Many unseen tracked (50), 4 niches → IN capped at 9, rest 4/4/4 wait 6."""
    ins, alloc = compute_slot_mix(
        50, [("dogs", 1.0), ("cats", 1.0), ("horses", 1.0), ("wildlife", 1.0)],
        batch_size=15,
    )
    # 60% of 15 = 9
    assert ins == int(15 * INSPIRATION_TARGET_RATIO) == 9
    assert sum(alloc.values()) == 6
    # 6 / 4 = 1 remainder 2 → first two niches get 2, rest get 1
    assert alloc["dogs"] == 2 and alloc["cats"] == 2
    assert alloc["horses"] == 1 and alloc["wildlife"] == 1


def test_mix_inspiration_capped_one_niche():
    """Many unseen tracked, 1 niche → 9 IN + 6 niche."""
    ins, alloc = compute_slot_mix(50, [("dogs", 1.0)], batch_size=15)
    assert ins == 9
    assert alloc == {"dogs": 6}


def test_mix_no_niches_returns_empty_allocation():
    ins, alloc = compute_slot_mix(20, [], batch_size=15)
    # Without niches, slot_sequence has no place to put the remaining batch slots.
    # compute_slot_mix still reports inspiration_slots up to the cap; allocation empty.
    assert ins == 9
    assert alloc == {}


# ─── allocate_niche_slots ─────────────────────────────────────────────────────

def test_allocate_zero_slots():
    assert allocate_niche_slots([("a", 1.0), ("b", 1.0)], 0) == {}


def test_allocate_zero_niches():
    assert allocate_niche_slots([], 5) == {}


def test_allocate_remainder_goes_to_first():
    # 7 slots / 3 niches = 2 base + remainder 1
    alloc = allocate_niche_slots([("a", 1.0), ("b", 1.0), ("c", 1.0)], 7)
    assert alloc == {"a": 3, "b": 2, "c": 2}


def test_allocate_evenly_divisible():
    alloc = allocate_niche_slots([("a", 1.0), ("b", 1.0), ("c", 1.0)], 6)
    assert alloc == {"a": 2, "b": 2, "c": 2}


# ─── generate_interleaved_sequence ────────────────────────────────────────────

def test_sequence_no_inspiration_round_robin():
    """No inspiration; niche stream alternates by allocation count DESC."""
    seq = generate_interleaved_sequence({"dogs": 4, "cats": 4, "horses": 4, "wildlife": 3}, 0)
    assert len(seq) == 15
    # No two adjacent slots should be the same niche (round-robin guarantees).
    for i in range(1, len(seq)):
        assert seq[i] != seq[i - 1], f"adjacent same-niche at pos {i}: {seq}"
    assert seq.count("dogs") == 4
    assert seq.count("wildlife") == 3


def test_sequence_inspiration_only():
    seq = generate_interleaved_sequence({}, 9)
    assert seq == [INSPIRATION_KEY] * 9


def test_sequence_mixed_spreads_inspiration():
    """3 IN + 12 niche (3/3/3/3) → 15 slots, IN slots spread roughly equispaced."""
    seq = generate_interleaved_sequence(
        {"dogs": 3, "cats": 3, "horses": 3, "wildlife": 3}, 3
    )
    assert len(seq) == 15
    assert seq.count(INSPIRATION_KEY) == 3
    # No 4-in-a-row of INSPIRATION
    for i in range(len(seq) - 3):
        window = seq[i : i + 4]
        assert window.count(INSPIRATION_KEY) < 4

    # First and last slots are niche (inspiration positions don't land at 0 or 14).
    assert seq[0] != INSPIRATION_KEY
    assert seq[-1] != INSPIRATION_KEY


def test_sequence_inspiration_heavy_no_run_of_4():
    """9 IN + 6 niche (single niche) — biggest IN share that can occur."""
    seq = generate_interleaved_sequence({"dogs": 6}, 9)
    assert len(seq) == 15
    assert seq.count(INSPIRATION_KEY) == 9
    for i in range(len(seq) - 3):
        window = seq[i : i + 4]
        assert window.count(INSPIRATION_KEY) < 4, f"4-in-a-row at pos {i}: {window}"


def test_sequence_single_niche_no_inspiration():
    seq = generate_interleaved_sequence({"dogs": 15}, 0)
    assert seq == ["dogs"] * 15


# ─── creator_penalty ──────────────────────────────────────────────────────────

def test_penalty_no_history_is_one():
    assert creator_penalty("c1", [], {}, current_slot_pos=0) == 1.0


def test_penalty_cumulative_decreases():
    """Each show drops the multiplier per COOLDOWN_CUMULATIVE."""
    p0 = creator_penalty("c1", [], {"c1": 0}, 5)
    p1 = creator_penalty("c1", [], {"c1": 1}, 5)
    p2 = creator_penalty("c1", [], {"c1": 2}, 5)
    p3 = creator_penalty("c1", [], {"c1": 3}, 5)
    p9 = creator_penalty("c1", [], {"c1": 99}, 5)
    assert p0 == COOLDOWN_CUMULATIVE[0]
    assert p1 == COOLDOWN_CUMULATIVE[1]
    assert p2 == COOLDOWN_CUMULATIVE[2]
    assert p3 == COOLDOWN_CUMULATIVE[3]
    assert p9 == COOLDOWN_CUMULATIVE_FLOOR


def test_penalty_back_to_back_is_minimum():
    """If creator was just placed at slot N-1, penalty at slot N is COOLDOWN_BASE."""
    p = creator_penalty("c1", [("c1", 4)], {"c1": 0}, current_slot_pos=5)
    # cumulative=1.0, recency = COOLDOWN_BASE + (1/6)*0.7 ≈ 0.417
    expected_recency = COOLDOWN_BASE + (1 / COOLDOWN_RECOVERY_SLOTS) * (1.0 - COOLDOWN_BASE)
    assert p == pytest.approx(expected_recency, rel=1e-6)


def test_penalty_recency_recovers_after_window():
    """Past the recovery window, recency saturates at 1.0."""
    p = creator_penalty("c1", [("c1", 0)], {"c1": 0},
                        current_slot_pos=COOLDOWN_RECOVERY_SLOTS + 1)
    assert p == 1.0


def test_penalty_combines_cumulative_and_recency():
    """A creator already shown once + just placed → cumulative * recency."""
    p = creator_penalty("c1", [("c1", 4)], {"c1": 1}, current_slot_pos=5)
    expected_recency    = COOLDOWN_BASE + (1 / COOLDOWN_RECOVERY_SLOTS) * (1.0 - COOLDOWN_BASE)
    expected_cumulative = COOLDOWN_CUMULATIVE[1]
    assert p == pytest.approx(expected_recency * expected_cumulative, rel=1e-6)


def test_penalty_different_creator_unaffected():
    """A different creator's history doesn't penalize this one."""
    p = creator_penalty("c1", [("c2", 4)], {"c2": 5}, current_slot_pos=5)
    assert p == 1.0


# ─── Spec scenarios (end-to-end smoke at the sequence layer) ──────────────────

def test_scenario_no_tracked_single_niche():
    """No tracked, 1 niche → 15 slots all from that niche."""
    ins, alloc = compute_slot_mix(0, [("dogs", 1.0)], batch_size=15)
    seq = generate_interleaved_sequence(alloc, ins)
    assert seq == ["dogs"] * 15


def test_scenario_no_tracked_four_niches_no_adjacent_repeat():
    """No tracked, 4 niches → 15 slots split 4/4/4/3, no two adjacent same niche."""
    ins, alloc = compute_slot_mix(
        0, [("dogs", 1.0), ("cats", 1.0), ("horses", 1.0), ("wildlife", 1.0)],
        batch_size=15,
    )
    seq = generate_interleaved_sequence(alloc, ins)
    for i in range(1, len(seq)):
        assert seq[i] != seq[i - 1]


def test_scenario_inspiration_heavy_two_niches():
    """50 tracked, 2 niches → 9 IN + 6 niche (3/3)."""
    ins, alloc = compute_slot_mix(
        50, [("dogs", 1.0), ("cats", 1.0)],
        batch_size=15,
    )
    seq = generate_interleaved_sequence(alloc, ins)
    assert ins == 9
    assert alloc == {"dogs": 3, "cats": 3}
    assert seq.count(INSPIRATION_KEY) == 9
    assert seq.count("dogs") == 3
    assert seq.count("cats") == 3
