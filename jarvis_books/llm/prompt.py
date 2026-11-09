"""Fit a request's prompt sections into the model's budget, trimming lowest-priority context first."""
from __future__ import annotations

from dataclasses import dataclass, field

from jarvis_books.llm.models import PromptSection
from jarvis_books.llm.sizing import estimate_tokens


@dataclass
class FitResult:
    text: str
    tokens: int
    trimmed: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


def _cut(text: str, max_tokens: int, chars_per_token: float, keep: str) -> str:
    max_chars = int(max_tokens * chars_per_token)
    if len(text) <= max_chars:
        return text
    if max_chars <= 40:
        return ""
    if keep == "end":  # keep the end (most recent), cut the start
        cut = text[-max_chars:]
        nl = cut.find("\n")
        return "[...earlier text omitted...]\n" + (cut[nl + 1:] if 0 <= nl < 200 else cut)
    cut = text[:max_chars]
    nl = cut.rfind("\n")
    return (cut[:nl] if nl > max_chars * 0.7 else cut) + "\n[...omitted...]"


def fit_sections(instruction: str, sections: list[PromptSection], budget_tokens: int,
                 chars_per_token: float = 4.0) -> FitResult:
    """Required sections and the instruction are never removed; others shrink then drop by priority."""
    rendered = {id(s): s.render() for s in sections}
    fixed = estimate_tokens(instruction, chars_per_token)
    total = fixed + sum(estimate_tokens(t, chars_per_token) for t in rendered.values())
    trimmed: list[str] = []
    dropped: list[str] = []
    if total > budget_tokens:
        for section in sorted((s for s in sections if not s.required), key=lambda s: s.priority):
            over = total - budget_tokens
            if over <= 0:
                break
            current = estimate_tokens(rendered[id(section)], chars_per_token)
            keep_tokens = current - over
            keep = "end" if section.trim_from == "start" else "start"
            if keep_tokens >= max(150, current * 0.15):
                rendered[id(section)] = f"## {section.name}\n" + _cut(section.text.strip(), keep_tokens - 10,
                                                                     chars_per_token, keep)
                trimmed.append(section.name)
            else:
                rendered[id(section)] = ""
                dropped.append(section.name)
            total = fixed + sum(estimate_tokens(t, chars_per_token) for t in rendered.values())
    order = sorted(sections, key=lambda s: -s.priority)
    body = "\n\n".join(t for t in (rendered[id(s)] for s in order) if t)
    text = (body + "\n\n" if body else "") + "## Task\n" + instruction.strip()
    return FitResult(text, estimate_tokens(text, chars_per_token), trimmed, dropped)
