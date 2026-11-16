"""Choosing which conversation turns go into a request.

The request being answered (its plain user turn plus every tool call and reply since) always goes whole, so a
long research run can never push the question out; the turn limit only applies to the earlier conversation.
When even that is too big, tool replies are shortened instead of dropped.
"""
from __future__ import annotations

import json
from typing import Callable


def plain_user(turn: dict) -> bool:
    return turn.get("role") == "user" and not any("functionResponse" in p for p in turn.get("parts", []))


def _start_of_current(history: list[dict]) -> int:
    return max((i for i, t in enumerate(history) if plain_user(t)), default=0)


def _drop_orphans(turns: list[dict]) -> list[dict]:
    while turns and not plain_user(turns[0]):
        turns = turns[1:]  # a window must start on a plain user turn (no orphaned tool replies)
    return turns


def window(history: list[dict] | None, turns: int, answering: bool = True) -> list[dict]:
    """answering=False: the request comes as a new instruction, so every turn here is earlier conversation."""
    full = list(history or [])
    current = _start_of_current(full) if answering else len(full)
    earlier = _drop_orphans(full[:current][-turns:]) if turns > 0 else []
    return _drop_orphans(earlier + full[current:])


def fit(history: list[dict], limit_tokens: int, cost: Callable[[dict], int], answering: bool = True) -> list[dict]:
    """Drop earlier conversation, then shorten tool replies, until the turns fit."""
    turns = list(history)
    used = sum(cost(t) for t in turns)
    current = _start_of_current(turns) if answering else len(turns)
    while used > limit_tokens and current > 0:
        turns = turns[1:]
        current -= 1
        dropped = _drop_orphans(turns)
        current -= len(turns) - len(dropped)
        turns = dropped
        used = sum(cost(t) for t in turns)
    cap = 4000
    while used > limit_tokens and cap >= 250:
        turns = [_shorten(t, cap) for t in turns]
        used = sum(cost(t) for t in turns)
        cap //= 2
    return turns


def _shorten(turn: dict, cap: int) -> dict:
    parts = []
    for p in turn.get("parts", []):
        reply = p.get("functionResponse")
        if reply is not None:
            raw = json.dumps(reply.get("response", {}), ensure_ascii=False)
            if len(raw) > cap:
                reply = {**reply, "response": {"shortened_result": raw[:cap] + " ..."}}
                p = {**p, "functionResponse": reply}
        parts.append(p)
    return {**turn, "parts": parts}
