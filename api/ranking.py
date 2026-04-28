"""Frame-level similarity scoring for the feed.

Per (user, video) pair we compute three components, all from frame-level
embeddings (no mean-pooling at scoring time — only ever for stage-1 ANN
candidate retrieval):

  semantic_sim  = mean of top-K cosine sims across user_siglip × video_siglip
                  frame matrix (M user frames × N video frames)
  structure_sim = same shape on user_dino × video_dino
  text_sim      = max cosine sim of user_text vector against video_siglip
                  frames (1 × N)

All inputs are pre-unit-normalized (onboarding + scrape pipeline both
normalize before storing). Cosine sim of unit vectors is a dot product.

Each component is then mapped from cosine [-1, 1] to [0, 1] via (sim + 1) / 2,
and combined: 0.6 * semantic + 0.2 * structure + 0.2 * text.

Implementation runs vectorized over a batch of V candidate videos at once.
For 200 candidates × 32 user frames × 8 video frames the einsum is trivial
(<50ms on CPU).
"""
from __future__ import annotations

import numpy as np

TOP_K = 5
SEMANTIC_WEIGHT = 0.6
STRUCTURE_WEIGHT = 0.2
TEXT_WEIGHT = 0.2


def _topk_mean(matrix: np.ndarray, k: int = TOP_K) -> np.ndarray:
    """Per-row top-k mean. matrix: (V, ...). Returns (V,).

    If a row has fewer than k values (small frame counts), falls back to mean.
    """
    flat = matrix.reshape(matrix.shape[0], -1)
    if flat.shape[1] <= k:
        return flat.mean(axis=1)
    topk = np.partition(flat, -k, axis=1)[:, -k:]
    return topk.mean(axis=1)


def _cosine_to_unit(sim: np.ndarray) -> np.ndarray:
    """Map cosine [-1, 1] → [0, 1] linearly."""
    return (sim + 1.0) / 2.0


def score_videos(
    user_siglip: np.ndarray,    # (M, 1152)
    user_dino: np.ndarray,      # (M, 768)
    user_text: np.ndarray,      # (1152,)
    video_siglip: np.ndarray,   # (V, N, 1152)
    video_dino: np.ndarray,     # (V, N, 768)
) -> dict[str, np.ndarray]:
    """Vectorized scoring across V candidate videos.

    Shapes assume all videos have N frames (right-pad upstream if not).
    Returns dict with arrays {final, semantic, structure, text}, each (V,).
    """
    if video_siglip.shape[0] == 0:
        empty = np.zeros((0,), dtype=np.float32)
        return {"final": empty, "semantic": empty, "structure": empty, "text": empty}

    # Pairwise cosine sims (since inputs are unit-norm, dot product == cosine).
    sim_siglip = np.einsum("mj,vfj->vmf", user_siglip, video_siglip)   # (V, M, N)
    sim_dino   = np.einsum("mj,vfj->vmf", user_dino,   video_dino)     # (V, M, N)
    sim_text   = np.einsum("j,vfj->vf",   user_text,   video_siglip)   # (V, N)

    semantic_raw  = _topk_mean(sim_siglip)
    structure_raw = _topk_mean(sim_dino)
    text_raw      = sim_text.max(axis=1)

    semantic  = _cosine_to_unit(semantic_raw).astype(np.float32)
    structure = _cosine_to_unit(structure_raw).astype(np.float32)
    text      = _cosine_to_unit(text_raw).astype(np.float32)

    final = (
        SEMANTIC_WEIGHT * semantic
        + STRUCTURE_WEIGHT * structure
        + TEXT_WEIGHT * text
    ).astype(np.float32)

    return {"final": final, "semantic": semantic, "structure": structure, "text": text}
