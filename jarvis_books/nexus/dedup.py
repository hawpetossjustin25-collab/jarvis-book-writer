"""Duplicate detection: an exact content hash, and a 64-bit SimHash for pages that are nearly the same."""
from __future__ import annotations

import hashlib
import re

NEAR_BITS = 3  # pages whose SimHashes differ in at most this many bits are near-duplicates
_WORD = re.compile(r"\w+", re.UNICODE)


def words(text: str) -> list[str]:
    return _WORD.findall((text or "").lower())


def content_hash(text: str) -> str:
    """Same words in the same order -> same hash, whatever the spacing or case."""
    return hashlib.sha256(" ".join(words(text)).encode("utf-8")).hexdigest()


def simhash(text: str, shingle: int = 3) -> int:
    """Charikar SimHash over word 3-shingles (unsigned 64-bit). 0 for text with no words."""
    tokens = words(text)
    if not tokens:
        return 0
    grams = [" ".join(tokens[i:i + shingle]) for i in range(max(1, len(tokens) - shingle + 1))]
    counts = [0] * 64
    for gram in grams:
        h = int.from_bytes(hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest(), "big")
        for bit in range(64):
            counts[bit] += 1 if h >> bit & 1 else -1
    return sum(1 << bit for bit in range(64) if counts[bit] > 0)


def hamming(a: int, b: int) -> int:
    return bin((a ^ b) & 0xFFFFFFFFFFFFFFFF).count("1")


def bands(h: int) -> tuple[int, int, int, int]:
    """Four 16-bit slices. Two hashes within 3 bits of each other share at least one slice exactly."""
    return tuple((h >> (16 * i)) & 0xFFFF for i in range(4))


def to_signed(h: int) -> int:
    """SQLite stores signed 64-bit integers."""
    return h - (1 << 64) if h >= 1 << 63 else h


def to_unsigned(h: int) -> int:
    return h + (1 << 64) if h < 0 else h
