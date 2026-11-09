"""Discover the models this API key can use and normalize their metadata into profiles."""
from __future__ import annotations

import logging
import re
from typing import Optional

from jarvis_books.llm.models import ModelProfile

log = logging.getLogger(__name__)

_NAME = re.compile(r"^gemini-(?P<gen>\d+(?:\.\d+)?)-(?P<tier>flash-lite|flash|pro)(?P<rest>.*)$")
_EXCLUDE = ("image", "omni", "customtools", "translate", "robotics", "computer-use", "deep-research")


def parse_name(model_id: str) -> tuple[Optional[float], Optional[str], str]:
    m = _NAME.match(model_id)
    if not m:
        return None, None, ""
    return float(m.group("gen")), m.group("tier"), m.group("rest")


def profile_from_metadata(raw: dict) -> Optional[ModelProfile]:
    model_id = str(raw.get("name", "")).removeprefix("models/")
    if not model_id.startswith("gemini"):
        return None
    methods = list(raw.get("supportedGenerationMethods") or [])
    gen, tier, rest = parse_name(model_id)
    lowered = model_id.lower()
    profile = ModelProfile(
        provider="gemini", model_id=model_id, version=raw.get("version"), display_name=raw.get("displayName"),
        tier=tier, generation=gen, preview="preview" in lowered or "exp" in lowered, methods=methods,
        context_window=raw.get("inputTokenLimit"), max_output_tokens=raw.get("outputTokenLimit"),
        supports_thinking=raw.get("thinking"), metadata_source="models.list", confidence=0.6,
    )
    profile.family = f"gemini-{tier}" if tier else None
    if "bidiGenerateContent" in methods:
        profile.supports_live = True
    if "embedContent" in methods or "embedding" in lowered:
        profile.supports_embedding = True
    if "tts" in lowered:
        profile.supports_audio_out = True
    if any(x in lowered for x in _EXCLUDE):
        profile.limitations.append("specialized model (not used for text roles)")
    if rest.startswith("-latest") or model_id.endswith("-latest"):
        profile.limitations.append("alias")
    # Streaming and system instructions are part of the generateContent API surface; tools/JSON stay
    # unknown until the capability probes verify them.
    if "generateContent" in methods:
        profile.supports_streaming = True
        profile.supports_system_messages = True
    return profile


def discover(raw_models: list[dict]) -> list[ModelProfile]:
    profiles = [p for p in (profile_from_metadata(m) for m in raw_models) if p]
    log.info("discovered %d Gemini models", len(profiles))
    return profiles


def text_candidates(profiles: list[ModelProfile]) -> list[ModelProfile]:
    """Text-generation models usable for writing, newest generation first, stable before preview."""
    out = [p for p in profiles if "generateContent" in p.methods and p.tier and p.kind == "text"
           and not any("specialized" in lim or lim == "alias" for lim in p.limitations)]
    out.sort(key=lambda p: (-(p.generation or 0), p.preview, p.model_id))
    return out


def best_of_kind(profiles: list[ModelProfile], kind: str) -> list[ModelProfile]:
    picked = [p for p in profiles if p.kind == kind and not any("specialized" in lim for lim in p.limitations)]
    picked.sort(key=lambda p: (p.preview, -(p.generation or _gen_from(p.model_id)),
                               "lite" in p.model_id, p.model_id))
    return picked


def _gen_from(model_id: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)", model_id)
    return float(m.group(1)) if m else 0.0
