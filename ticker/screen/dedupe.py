"""Stage 0 — near-duplicate suppression.

The largest single volume reduction in the pipeline, and the cheapest. One AP
story is republished verbatim by hundreds of outlets; without near-dupe
detection the funnel spends all day rescanning the same sentence, and the
evidence accumulator mistakes syndication for consensus.

SimHash + banded LSH: 64-bit fingerprint, 4 bands of 16 bits. Two texts within
`hamming_threshold` bits must agree on at least one band, so band lookup gives
us candidates in O(1) instead of comparing against every hash seen.
"""

from __future__ import annotations

import hashlib
import re
import time

_WORD = re.compile(r"[a-z0-9']+")
_BANDS = 4
_BAND_BITS = 16
_BAND_MASK = (1 << _BAND_BITS) - 1


def _shingles(text: str, k: int = 3) -> list[str]:
    words = _WORD.findall(text.lower())
    if len(words) < k:
        return [" ".join(words)] if words else []
    return [" ".join(words[i:i + k]) for i in range(len(words) - k + 1)]


def simhash(text: str) -> int:
    """64-bit SimHash over word trigrams."""
    vec = [0] * 64
    for sh in _shingles(text):
        h = int.from_bytes(hashlib.blake2b(sh.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            vec[bit] += 1 if (h >> bit) & 1 else -1
    out = 0
    for bit in range(64):
        if vec[bit] > 0:
            out |= 1 << bit
    return out


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


class Deduper:
    def __init__(
        self,
        ttl_s: float = 3600.0,
        hamming_threshold: int = 3,
        max_entries: int = 50_000,
    ) -> None:
        self.ttl_s = ttl_s
        self.threshold = hamming_threshold
        self.max_entries = max_entries
        self._bands: list[dict[int, list[tuple[int, float]]]] = [
            {} for _ in range(_BANDS)
        ]
        self._count = 0
        self.stats = {"seen": 0, "duplicates": 0, "evictions": 0}

    def reset(self) -> None:
        """Drop all state. Used by tests and by tools/replay.py between runs."""
        self._bands = [{} for _ in range(_BANDS)]
        self._count = 0

    def _band_keys(self, h: int) -> list[int]:
        return [(h >> (i * _BAND_BITS)) & _BAND_MASK for i in range(_BANDS)]

    def check_and_add(self, text: str) -> bool:
        """True if this is a near-duplicate of something already seen.

        Non-duplicates are recorded, so this is the only call the funnel needs.
        """
        self.stats["seen"] += 1
        now = time.monotonic()
        h = simhash(text)
        keys = self._band_keys(h)

        for band, key in zip(self._bands, keys):
            bucket = band.get(key)
            if not bucket:
                continue
            for other, expiry in bucket:
                if expiry > now and hamming(h, other) <= self.threshold:
                    self.stats["duplicates"] += 1
                    return True

        expiry = now + self.ttl_s
        for band, key in zip(self._bands, keys):
            band.setdefault(key, []).append((h, expiry))
        self._count += 1

        if self._count > self.max_entries:
            self._sweep(now)
        return False

    def _sweep(self, now: float) -> None:
        """Drop expired entries. Amortised — only runs when over capacity."""
        remaining = 0
        for band in self._bands:
            for key in list(band.keys()):
                kept = [e for e in band[key] if e[1] > now]
                if kept:
                    band[key] = kept
                else:
                    del band[key]
            remaining = max(remaining, sum(len(v) for v in band.values()))
        self.stats["evictions"] += max(0, self._count - remaining)
        self._count = remaining
        if self._count > self.max_entries:
            # Still over budget after expiry sweep: TTL is too long for the
            # observed volume. Hard reset beats unbounded memory on a 911 MB box.
            self._bands = [{} for _ in range(_BANDS)]
            self._count = 0
