from __future__ import annotations

import hashlib
import math


def deterministic_embedding(text: str, model_id: str, dimensions: int = 1024) -> list[float]:
    """Return a deterministic, local-only unit vector for validation tests."""
    if dimensions <= 0:
        raise ValueError("dimensions must be positive")

    normalized = " ".join(text.casefold().split())
    features = list(normalized)
    features.extend(normalized[index : index + 3] for index in range(max(0, len(normalized) - 2)))
    if not features:
        features = [""]

    vector = [0.0] * dimensions
    for feature in features:
        digest = hashlib.sha256(f"{model_id}\0{feature}".encode()).digest()
        index = int.from_bytes(digest[:8], "big") % dimensions
        vector[index] += 1.0 if digest[8] & 1 else -1.0

    magnitude = math.sqrt(sum(value * value for value in vector))
    return [value / magnitude for value in vector]
