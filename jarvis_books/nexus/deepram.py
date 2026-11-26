"""DeepRAM: plans a model's memory before it is loaded, so a model that won't fit is refused instead of freezing
the computer. The plan adds up what really takes memory: the weights (the model file), the context cache
(key/value vectors for every layer and every token of context), the runtime's own overhead and scratch space, then
keeps a safety margin for the rest of the computer. Inputs come from the model's own metadata (Ollama /api/show),
not from guesses. Memory on another computer is never counted as if it were local RAM.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

GB = 1024 ** 3
MB = 1024 ** 2
CACHE_BYTES = {"f16": 2.0, "q8_0": 1.0625, "q4_0": 0.5625}  # bytes per value of the key/value cache
RUNTIME_OVERHEAD = 350 * MB  # llama.cpp/Ollama process, graph and buffers measured around 300-400 MB on CPU
SCRATCH_PER_1K_CTX = 24 * MB  # compute buffers grow with the batch/context


@dataclass
class ModelShape:
    """What the model file says about itself."""
    name: str
    weights_bytes: int
    layers: Optional[int] = None
    kv_heads: Optional[int] = None
    heads: Optional[int] = None
    embedding: Optional[int] = None
    key_length: Optional[int] = None
    value_length: Optional[int] = None
    max_context: Optional[int] = None
    parameters: Optional[int] = None
    quantization: str = ""
    architecture: str = ""

    @classmethod
    def from_ollama(cls, name: str, show: dict, size_bytes: int) -> "ModelShape":
        info = show.get("model_info") or {}
        arch = info.get("general.architecture") or (show.get("details") or {}).get("family") or ""

        def num(key: str) -> Optional[int]:
            v = info.get(f"{arch}.{key}")
            if isinstance(v, list):  # some models list a value per layer
                v = max(v) if v else None
            try:
                return int(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        return cls(name=name, weights_bytes=int(size_bytes), layers=num("block_count"),
                   kv_heads=num("attention.head_count_kv") or num("attention.head_count"),
                   heads=num("attention.head_count"), embedding=num("embedding_length"),
                   key_length=num("attention.key_length"), value_length=num("attention.value_length"),
                   max_context=num("context_length"),
                   parameters=_int(info.get("general.parameter_count")),
                   quantization=(show.get("details") or {}).get("quantization_level", ""), architecture=arch)

    def kv_bytes_per_token(self, cache_type: str = "f16") -> Optional[float]:
        if not (self.layers and self.kv_heads):
            return None
        head = self.key_length or (self.embedding // self.heads if self.embedding and self.heads else None)
        vhead = self.value_length or head
        if not head:
            return None
        return self.layers * self.kv_heads * (head + vhead) * CACHE_BYTES[cache_type]


@dataclass
class MemoryPlan:
    model: str
    context: int
    weights_gb: float
    cache_gb: Optional[float]
    overhead_gb: float
    scratch_gb: float
    total_gb: Optional[float]
    available_gb: Optional[float]
    margin_gb: float
    fits: bool
    reason: str
    known: bool = True
    alternatives: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class DeepRAM:
    def __init__(self, margin_gb: float = 1.5, margin_fraction: float = 0.10, cache_type: str = "f16"):
        if cache_type not in CACHE_BYTES:
            raise ValueError(f"cache type is one of {', '.join(CACHE_BYTES)}")
        self.margin_gb, self.margin_fraction, self.cache_type = margin_gb, margin_fraction, cache_type

    def margin(self, total_gb: Optional[float]) -> float:
        return round(max(self.margin_gb, (total_gb or 0) * self.margin_fraction), 2)

    def plan(self, shape: ModelShape, context: int, available_bytes: Optional[int],
             total_bytes: Optional[int] = None, already_loaded: bool = False) -> MemoryPlan:
        if context < 1:
            raise ValueError("context must be at least 1 token")
        if shape.max_context and context > shape.max_context:
            context = shape.max_context
        per_token = shape.kv_bytes_per_token(self.cache_type)
        weights = 0 if already_loaded else shape.weights_bytes
        cache = per_token * context if per_token is not None else None
        overhead = 0 if already_loaded else RUNTIME_OVERHEAD
        scratch = SCRATCH_PER_1K_CTX * max(1.0, context / 1024)
        total = (weights + cache + overhead + scratch) if cache is not None else None
        avail_gb = available_bytes / GB if available_bytes is not None else None
        margin = self.margin(total_bytes / GB if total_bytes else None)
        if total is None:
            # layer data is missing: the cache can't be sized, so assume it is as big as the weights
            guess = weights * 2 + overhead + scratch
            fits = avail_gb is not None and guess / GB + margin <= avail_gb
            reason = ("the model doesn't say how many layers it has, so its context memory was assumed as large "
                      "as the model itself" + ("" if fits else "; that doesn't fit"))
            return MemoryPlan(shape.name, context, _gb(weights), None, _gb(overhead), _gb(scratch), _gb(guess),
                              _round(avail_gb), margin, fits, reason, known=False)
        if avail_gb is None:
            return MemoryPlan(shape.name, context, _gb(weights), _gb(cache), _gb(overhead), _gb(scratch), _gb(total),
                              None, margin, False, "free memory couldn't be read, so loading isn't safe to promise")
        need = total / GB + margin
        fits = need <= avail_gb
        reason = (f"needs {total / GB:.1f} GB plus {margin:g} GB to spare; {avail_gb:.1f} GB is free" if fits else
                  f"needs {total / GB:.1f} GB plus {margin:g} GB to spare, but only {avail_gb:.1f} GB is free")
        return MemoryPlan(shape.name, context, _gb(weights), _gb(cache), _gb(overhead), _gb(scratch), _gb(total),
                          _round(avail_gb), margin, fits, reason)

    def largest_context(self, shape: ModelShape, available_bytes: Optional[int], total_bytes: Optional[int] = None,
                        floor: int = 2048, already_loaded: bool = False) -> Optional[int]:
        """The longest context (a multiple of 1024, at least `floor`) that still fits now, or None."""
        top = shape.max_context or 131072
        best = None
        lo, hi = max(1, floor // 1024), max(1, top // 1024)
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.plan(shape, mid * 1024, available_bytes, total_bytes, already_loaded).fits:
                best, lo = mid * 1024, mid + 1
            else:
                hi = mid - 1
        return best

    def choose(self, shapes: list[ModelShape], context: int, available_bytes: Optional[int],
               total_bytes: Optional[int] = None, loaded: tuple[str, ...] = ()) -> list[MemoryPlan]:
        """Every candidate's plan, the ones that fit first (largest model that fits first)."""
        plans = [self.plan(s, context, available_bytes, total_bytes, s.name in loaded) for s in shapes]
        fitting = [p.model for p in plans if p.fits]
        for p in plans:
            if not p.fits:
                p.alternatives = fitting[:3]
                if not fitting:
                    p.reason += ("; try a smaller or more compressed model, a shorter context, or run it on "
                                 "another computer in the Collective")
        by_size = {s.name: s.weights_bytes for s in shapes}
        return sorted(plans, key=lambda p: (not p.fits, -by_size[p.model]))


def _int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _gb(b: Optional[float]) -> Optional[float]:
    return round(b / GB, 3) if b is not None else None


def _round(v: Optional[float]) -> Optional[float]:
    return round(v, 2) if v is not None else None
