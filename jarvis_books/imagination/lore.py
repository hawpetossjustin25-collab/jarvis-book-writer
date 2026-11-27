"""Answer questions about Imagination from its canon: the story bible entries and the story canon text."""
from __future__ import annotations

from jarvis_books.imagination.bible import _entries
from jarvis_books.imagination.canon import Canon, lore_text, norm

MAX_FACTS = 8


def lookup(canon: Canon, topic: str) -> dict:
    words = [w for w in norm(topic).split() if len(w) > 2]
    if not words:
        return {"error": "Tell me what about Imagination you want to know."}
    scored = []
    for kind, name, summary, _ in _entries(canon):
        hay_name, hay = norm(name), norm(summary)
        score = sum(3 for w in words if w in hay_name.split()) + sum(1 for w in words if w in hay)
        if score:
            scored.append((score, kind, name, summary))
    paragraphs = [p.strip() for p in lore_text().split("\n\n") if p.strip() and not p.startswith("#")]
    for p in paragraphs:
        score = sum(1 for w in words if w in norm(p))
        if score >= max(1, len(words) - 1):
            scored.append((score - 0.5, "canon", "Story canon", p))
    scored.sort(key=lambda x: -x[0])
    facts = [{"kind": k, "name": n, "fact": s} for _, k, n, s in scored[:MAX_FACTS]]
    if not facts:
        return {"topic": topic, "facts": [], "say": f"Nothing in the canon of Imagination mentions '{topic}'."}
    return {"topic": topic, "facts": facts, "say": facts[0]["fact"]}
