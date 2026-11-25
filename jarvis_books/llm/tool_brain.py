"""Jarvis's tool brain: a small model that turns a request into a tool call in a few milliseconds on the CPU.

It works coarse to fine, the way Next-Concept Modeling does: level 1 picks the tool (the "concept"); level 2
labels every word of the request at once with the argument it belongs to, so names, paths and numbers are
copied exactly; level 3 picks the choice arguments (enums and yes/no) the request implies. Only numpy is
needed, and the weights are a single .npz file trained from the app's own tool declarations (tool_data.py).
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from jarvis_books.llm.models import LLMRequest, LLMResponse
from jarvis_books.llm.schema import validate
from jarvis_books.llm.tool_data import (NO_TOOL, TEMPLATES_PATH, Example, generate, load_templates,
                                        parse_number)
from jarvis_books.llm.tool_eval import ToolCase

MODEL_NAME = "jarvis-tool-brain"
NONE = "<none>"
_TOKEN = re.compile(r"https?://\S+|[A-Za-z]:\\\S+|\$?\d[\d,]*(?:\.\d+)?k?\b|[\w][\w'.\\/:+\-]*|[^\s\w]")


def tokenize(text: str) -> list[tuple[str, int, int]]:
    out = []
    for m in _TOKEN.finditer(text):
        a, b = m.start(), m.end()
        while b - a > 1 and text[b - 1] in ".,?!:;'\")":
            b -= 1
        out.append((text[a:b], a, b))
    return out


def _shape(tok: str) -> str:
    if tok.startswith("http"):
        return "url"
    if re.match(r"^\$?\d", tok):
        return "num"
    if "\\" in tok or "/" in tok or re.search(r"\.\w{2,4}$", tok):
        return "path"
    return "cap" if tok[:1].isupper() else "low"


class Featurizer:
    def __init__(self, buckets: int):
        self.buckets = buckets

    def _h(self, s: str) -> int:
        return zlib.crc32(s.encode("utf-8")) % self.buckets

    def sentence(self, words: list[str]) -> list[int]:
        feats = ["<bias>"] + [f"w:{w}" for w in words] + [f"b:{a}_{b}" for a, b in zip(words, words[1:])]
        for w in words:
            padded = f"^{w}$"
            feats += [f"c:{padded[i:i + 3]}" for i in range(len(padded) - 2)]
        return [self._h(f) for f in feats]

    def token(self, words: list[str], shapes: list[str], i: int) -> list[int]:
        def at(j: int) -> str:
            return words[j] if 0 <= j < len(words) else ("<s>" if j < 0 else "</s>")
        w = words[i]
        padded = f"^{w}$"
        feats = ["<tbias>", f"t:{w}", f"s:{shapes[i]}", f"p1:{at(i - 1)}", f"p2:{at(i - 2)}", f"n1:{at(i + 1)}",
                 f"n2:{at(i + 2)}", f"pp:{at(i - 2)}_{at(i - 1)}", f"nn:{at(i + 1)}_{at(i + 2)}",
                 f"ps:{at(i - 1)}_{shapes[i]}", f"pos:{min(i, 12)}"]
        feats += [f"tc:{padded[k:k + 3]}" for k in range(len(padded) - 2)]
        return [self._h(f) for f in feats]


def _adam(params: dict, grads: dict, state: dict, lr: float, step: int, b1=0.9, b2=0.999, eps=1e-8) -> None:
    for k, g in grads.items():
        m, v = state.setdefault(k, (np.zeros_like(params[k]), np.zeros_like(params[k])))
        m *= b1
        m += (1 - b1) * g
        v *= b2
        v += (1 - b2) * g * g
        params[k] -= lr * (m / (1 - b1 ** step)) / (np.sqrt(v / (1 - b2 ** step)) + eps)


def _softmax(x: np.ndarray) -> np.ndarray:
    x = x - x.max(axis=-1, keepdims=True)
    e = np.exp(x)
    return e / e.sum(axis=-1, keepdims=True)


@dataclass
class Prediction:
    tool: Optional[str]
    args: dict
    confidence: float
    alternatives: list[tuple[str, float]]


class ToolBrain:
    def __init__(self, meta: dict, params: dict):
        self.meta, self.p = meta, params
        self.tools: list[str] = meta["tools"]  # index 0 is "no tool"
        self.keys: list[str] = meta["keys"]  # copied arguments, across all tools
        self.options: list[str] = meta["options"]  # "tool.arg=value" for choice arguments
        self.copy_args: dict[str, list[str]] = meta["copy_args"]
        self.choice_args: dict[str, dict[str, list[str]]] = meta["choice_args"]
        self.types: dict[str, dict[str, str]] = meta["types"]
        self.f = Featurizer(meta["buckets"])
        self._tool_ix = {t: i for i, t in enumerate(self.tools)}
        self._opt_ix = {o: i for i, o in enumerate(self.options)}
        self._label_mask = np.full((len(self.tools), 1 + 2 * len(self.keys)), -1e9, dtype=np.float32)
        for t, i in self._tool_ix.items():
            self._label_mask[i, 0] = 0
            for k in self.copy_args.get(t, []):
                j = self.keys.index(k)
                self._label_mask[i, 1 + 2 * j:3 + 2 * j] = 0

    # ---- building ---------------------------------------------------------------------
    @classmethod
    def fresh(cls, declarations: list[dict], hidden: int = 64, buckets: int = 1 << 16, seed: int = 0) -> "ToolBrain":
        tools, keys, options, copy_args, choice_args, types = [NO_TOOL], [], [], {}, {}, {}
        for d in declarations:
            tools.append(d["name"])
            props = d["parameters"].get("properties", {})
            types[d["name"]] = {k: v.get("type", "STRING") for k, v in props.items()}
            for k, v in props.items():
                if v.get("enum") or v.get("type") == "BOOLEAN":
                    values = [str(x) for x in v["enum"]] if v.get("enum") else ["true", "false"]
                    choice_args.setdefault(d["name"], {})[k] = values
                    options += [f"{d['name']}.{k}={x}" for x in values + [NONE]]
                else:
                    copy_args.setdefault(d["name"], []).append(k)
                    if k not in keys:
                        keys.append(k)
        rng = np.random.default_rng(seed)
        h, t, l, o = hidden, len(tools), 1 + 2 * len(keys), max(1, len(options))

        def w(*shape, scale=None):
            return (rng.standard_normal(shape) * (scale or 1 / np.sqrt(shape[0]))).astype(np.float32)
        params = {"E": w(buckets, h, scale=0.05), "W1": w(h, h), "b1": np.zeros(h, np.float32), "W2": w(h, t),
                  "b2": np.zeros(t, np.float32), "U": w(t, h, scale=0.1), "V1": w(3 * h, h),
                  "c1": np.zeros(h, np.float32), "V2": w(h, l), "c2": np.zeros(l, np.float32), "Q1": w(2 * h, h),
                  "q1": np.zeros(h, np.float32), "Q2": w(h, o), "q2": np.zeros(o, np.float32)}
        meta = {"version": 1, "hidden": h, "buckets": buckets, "tools": tools, "keys": keys, "options": options,
                "copy_args": copy_args, "choice_args": choice_args, "types": types}
        return cls(meta, params)

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        arrays = {k: v.astype(np.float16) for k, v in self.p.items()}
        tmp = path.with_name(path.name + ".tmp.npz")
        np.savez_compressed(tmp, meta=np.frombuffer(json.dumps(self.meta).encode("utf-8"), dtype=np.uint8), **arrays)
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "ToolBrain":
        with np.load(Path(path)) as data:
            meta = json.loads(bytes(data["meta"]).decode("utf-8"))
            params = {k: data[k].astype(np.float32) for k in data.files if k != "meta"}
        return cls(meta, params)

    # ---- encoding ---------------------------------------------------------------------
    def _encode(self, text: str):
        toks = tokenize(text)
        words = [t[0].lower() for t in toks]
        shapes = [_shape(t[0]) for t in toks]
        sent = self.f.sentence(words)
        per_tok = [self.f.token(words, shapes, i) for i in range(len(toks))]
        return toks, sent, per_tok

    def _labels(self, ex: Example, toks) -> list[int]:
        labels = [0] * len(toks)
        for key, (a, b) in ex.spans.items():
            if key not in self.keys or ex.tool is None or key not in self.copy_args.get(ex.tool, []):
                continue
            j = self.keys.index(key)
            first = True
            for i, (_, ta, tb) in enumerate(toks):
                if ta >= a and tb <= b:
                    labels[i] = 1 + 2 * j + (0 if first else 1)
                    first = False
        return labels

    # ---- training ---------------------------------------------------------------------
    def train(self, examples: list[Example], epochs: int = 12, batch: int = 32, lr: float = 0.01, seed: int = 0,
              progress: Callable[[float, str], None] = lambda p, m: None,
              cancel: Optional[Callable[[], bool]] = None) -> list[float]:
        rng = np.random.default_rng(seed)
        data = []
        for ex in examples:
            toks, sent, per_tok = self._encode(ex.request)
            tool = self._tool_ix.get(ex.tool or NO_TOOL)
            if tool is None or not toks:
                continue
            groups = []
            for arg, values in self.choice_args.get(ex.tool or "", {}).items():
                want = ex.args.get(arg)
                value = NONE if want is None else (str(want).lower() if isinstance(want, bool) else str(want))
                ids = [self._opt_ix[f"{ex.tool}.{arg}={x}"] for x in values + [NONE]]
                target = self._opt_ix.get(f"{ex.tool}.{arg}={value}")
                if target is not None:
                    groups.append((ids, ids.index(target)))
            data.append((tool, sent, per_tok, self._labels(ex, toks), groups))
        state, step, losses = {}, 0, []
        total = epochs * ((len(data) + batch - 1) // batch)
        for epoch in range(epochs):
            order = rng.permutation(len(data))
            run = 0.0
            for start in range(0, len(data), batch):
                if cancel is not None and cancel():
                    return losses
                step += 1
                loss, grads = self._step([self._drop(data[i], rng) for i in order[start:start + batch]])
                _adam(self.p, grads, state, lr * (0.5 if epoch >= epochs * 2 // 3 else 1.0), step)
                run += loss
                progress(step / total, f"training the tool brain: pass {epoch + 1} of {epochs}")
            losses.append(run / max(1, (len(data) + batch - 1) // batch))
        return losses

    @staticmethod
    def _drop(item, rng, rate: float = 0.25):
        """Training only: hide a quarter of each word's features so the brain learns from context too, not just
        from names it has seen."""
        tool, sent, per_tok, labels, groups = item
        keep = lambda ids: [x for x in ids if rng.random() > rate] or ids[:1]  # noqa: E731
        return tool, keep(sent), [keep(ids) for ids in per_tok], labels, groups

    def _step(self, items) -> tuple[float, dict]:
        p, h = self.p, self.meta["hidden"]
        n = len(items)
        # level 0: the request as a bag of hashed features
        s_ids = [np.array(it[1]) for it in items]
        S = np.stack([p["E"][ids].mean(0) for ids in s_ids])
        tools = np.array([it[0] for it in items])
        # level 1: the tool
        H1 = np.tanh(S @ p["W1"] + p["b1"])
        P = _softmax(H1 @ p["W2"] + p["b2"])
        loss = -np.log(P[np.arange(n), tools] + 1e-9).mean()
        dlog = P.copy()
        dlog[np.arange(n), tools] -= 1
        dlog /= n
        g = {"W2": H1.T @ dlog, "b2": dlog.sum(0)}
        dH1 = (dlog @ p["W2"].T) * (1 - H1 ** 2)
        g["W1"], g["b1"] = S.T @ dH1, dH1.sum(0)
        dS = dH1 @ p["W1"].T
        Ut = p["U"][tools]
        # level 2: every word's argument label, all at once
        t_rows, t_ids, labels = [], [], []
        for r, it in enumerate(items):
            for ids, lab in zip(it[2], it[3]):
                t_rows.append(r)
                t_ids.append(np.array(ids))
                labels.append(lab)
        rows = np.array(t_rows)
        T = np.stack([p["E"][ids].mean(0) for ids in t_ids])
        X = np.concatenate([T, S[rows], Ut[rows]], axis=1)
        Z = np.tanh(X @ p["V1"] + p["c1"])
        logits = Z @ p["V2"] + p["c2"] + self._label_mask[tools[rows]]
        Pl = _softmax(logits)
        labels = np.array(labels)
        m = len(labels)
        loss += -np.log(Pl[np.arange(m), labels] + 1e-9).mean()
        dl = Pl.copy()
        dl[np.arange(m), labels] -= 1
        dl /= m
        g["V2"], g["c2"] = Z.T @ dl, dl.sum(0)
        dZ = (dl @ p["V2"].T) * (1 - Z ** 2)
        g["V1"], g["c1"] = X.T @ dZ, dZ.sum(0)
        dX = dZ @ p["V1"].T
        dT = dX[:, :h]
        np.add.at(dS, rows, dX[:, h:2 * h])
        dU = np.zeros_like(p["U"])
        np.add.at(dU, tools[rows], dX[:, 2 * h:])
        # level 3: the choice arguments
        g_rows, g_ids, g_target = [], [], []
        for r, it in enumerate(items):
            for ids, target in it[4]:
                g_rows.append(r)
                g_ids.append(ids)
                g_target.append(target)
        g["Q1"], g["q1"] = np.zeros_like(p["Q1"]), np.zeros_like(p["q1"])
        g["Q2"], g["q2"] = np.zeros_like(p["Q2"]), np.zeros_like(p["q2"])
        if g_rows:
            gr = np.array(g_rows)
            Xg = np.concatenate([S[gr], Ut[gr]], axis=1)
            G = np.tanh(Xg @ p["Q1"] + p["q1"])
            full = G @ p["Q2"] + p["q2"]
            k = len(g_rows)
            dfull = np.zeros_like(full)
            for i, (ids, target) in enumerate(zip(g_ids, g_target)):
                pr = _softmax(full[i, ids])
                loss += -np.log(pr[target] + 1e-9) / k
                pr[target] -= 1
                dfull[i, ids] = pr / k
            g["Q2"], g["q2"] = G.T @ dfull, dfull.sum(0)
            dG = (dfull @ p["Q2"].T) * (1 - G ** 2)
            g["Q1"], g["q1"] = Xg.T @ dG, dG.sum(0)
            dXg = dG @ p["Q1"].T
            np.add.at(dS, gr, dXg[:, :h])
            np.add.at(dU, tools[gr], dXg[:, h:])
        g["U"] = dU
        dE = np.zeros_like(p["E"])
        for ids, d in zip(s_ids, dS):
            np.add.at(dE, ids, d / len(ids))
        for ids, d in zip(t_ids, dT):
            np.add.at(dE, ids, d / len(ids))
        g["E"] = dE
        return float(loss), g

    # ---- answering --------------------------------------------------------------------
    def predict(self, text: str, allowed: Optional[set] = None) -> Prediction:
        p = self.p
        toks, sent, per_tok = self._encode(text)
        if not toks:
            return Prediction(None, {}, 1.0, [])
        s = p["E"][sent].mean(0)
        probs = _softmax(np.tanh(s @ p["W1"] + p["b1"]) @ p["W2"] + p["b2"])
        if allowed is not None:
            keep = np.array([t == NO_TOOL or t in allowed for t in self.tools])
            probs = np.where(keep, probs, 0.0)
            probs = probs / probs.sum()
        order = np.argsort(-probs)
        best = int(order[0])
        alternatives = [(self.tools[i], round(float(probs[i]), 4)) for i in order[:3]]
        tool = self.tools[best]
        if tool == NO_TOOL:
            return Prediction(None, {}, float(probs[best]), alternatives)
        u = p["U"][best]
        T = np.stack([p["E"][ids].mean(0) for ids in per_tok])
        X = np.concatenate([T, np.tile(s, (len(toks), 1)), np.tile(u, (len(toks), 1))], axis=1)
        lab = _softmax(np.tanh(X @ p["V1"] + p["c1"]) @ p["V2"] + p["c2"] + self._label_mask[best])
        args: dict = {}
        confidence = float(probs[best])
        for j, key in enumerate(self.keys):
            if key not in self.copy_args.get(tool, []):
                continue
            span = self._best_span(lab[:, 1 + 2 * j], lab[:, 2 + 2 * j], lab[:, 0])
            if span is None:
                continue
            a, b = toks[span[0]][1], toks[span[1]][2]
            value = self._typed(tool, key, text[a:b])
            if value is not None:
                args[key] = value
        choices = self.choice_args.get(tool, {})
        if choices:
            gvec = np.tanh(np.concatenate([s, u]) @ p["Q1"] + p["q1"]) @ p["Q2"] + p["q2"]
            for arg, values in choices.items():
                names = values + [NONE]
                ids = [self._opt_ix[f"{tool}.{arg}={x}"] for x in names]
                pick = names[int(np.argmax(gvec[ids]))]
                if pick != NONE:
                    args[arg] = (pick == "true") if self.types[tool].get(arg) == "BOOLEAN" else pick
        return Prediction(tool, args, confidence, alternatives)

    @staticmethod
    def _best_span(begin: np.ndarray, inside: np.ndarray, outside: np.ndarray) -> Optional[tuple[int, int]]:
        best, best_score = None, 0.5
        for i in range(len(begin)):
            if begin[i] < 0.3 or begin[i] < outside[i] * 0.5:
                continue
            j = i
            while j + 1 < len(inside) and inside[j + 1] > outside[j + 1] and inside[j + 1] >= begin[j + 1]:
                j += 1
            score = float(np.mean(np.concatenate([[begin[i]], inside[i + 1:j + 1]])))
            if score > best_score:
                best, best_score = (i, j), score
        return best

    def _typed(self, tool: str, key: str, raw: str):
        kind = self.types.get(tool, {}).get(key, "STRING")
        raw = raw.strip().strip(".,?!")
        if kind in ("INTEGER", "NUMBER"):
            n = parse_number(raw)
            if n is None:
                return None
            return int(n) if kind == "INTEGER" else n
        return raw


class ToolBrainEngine:
    """The tool brain behind the same generate() every other brain has, so it can be tested and used like one.
    It only answers with a tool call it is sure of and that passes the tool's own schema; otherwise it says
    nothing and leaves the request for a bigger brain."""

    def __init__(self, brain: ToolBrain, min_confidence: float = 0.6, clock: Callable[[], float] = time.monotonic):
        self.brain, self.min_confidence, self.clock = brain, min_confidence, clock

    def generate(self, request: LLMRequest) -> LLMResponse:
        start = self.clock()
        text = ""
        for turn in reversed(request.history or []):
            if turn.get("role") == "user":
                text = " ".join(part.get("text", "") for part in turn.get("parts", []) if "text" in part).strip()
                if text:
                    break
        offered = {d["name"]: d for d in (request.tools or [])}
        pred = self.brain.predict(text or request.instruction, allowed=set(offered) if offered else None)
        calls, finish = [], "STOP"
        if pred.tool is not None and pred.confidence >= self.min_confidence:
            decl = offered.get(pred.tool)
            errors = validate(pred.args, decl["parameters"]) if decl else []
            if not errors:
                calls = [{"name": pred.tool, "args": pred.args}]
            else:
                finish = "UNSURE"
        elif pred.tool is not None:
            finish = "UNSURE"
        return LLMResponse(text="", model=MODEL_NAME, function_calls=calls, finish_reason=finish,
                           latency_s=self.clock() - start,
                           data={"confidence": round(pred.confidence, 4), "alternatives": pred.alternatives,
                                 "guess": {"name": pred.tool, "args": pred.args}})


def fingerprint(declarations: list[dict], templates_path: Path = TEMPLATES_PATH, hidden: int = 64) -> str:
    """Changes whenever a tool, an argument or a training pattern changes, so the brain is retrained to match."""
    blob = json.dumps(sorted(declarations, key=lambda d: d["name"]), sort_keys=True)
    blob += Path(templates_path).read_text(encoding="utf-8") + f"|v1|{hidden}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def train_tool_brain(declarations: list[dict], held_out: Optional[list[ToolCase]] = None,
                     templates_path: Path = TEMPLATES_PATH, per_template: int = 30, epochs: int = 10,
                     hidden: int = 64, buckets: int = 1 << 16, seed: int = 7,
                     progress: Callable[[float, str], None] = lambda p, m: None,
                     cancel: Optional[Callable[[], bool]] = None) -> ToolBrain:
    known = {d["name"] for d in declarations}
    templates = [t for t in load_templates(templates_path) if t.tool is None or t.tool in known]
    examples = generate(templates, per_template=per_template, seed=seed, held_out=held_out)
    brain = ToolBrain.fresh(declarations, hidden=hidden, buckets=buckets, seed=seed)
    brain.train(examples, epochs=epochs, seed=seed, progress=progress, cancel=cancel)
    brain.meta["fingerprint"] = fingerprint(declarations, templates_path, hidden)
    brain.meta["examples"] = len(examples)
    return brain


def ensure_tool_brain(folder: Path, declarations: list[dict], held_out: Optional[list[ToolCase]] = None,
                      **train_kwargs) -> ToolBrain:
    """Load the saved brain if it matches today's tools, otherwise train a new one and save it."""
    path = Path(folder) / "tool_brain.npz"
    want = fingerprint(declarations, train_kwargs.get("templates_path", TEMPLATES_PATH),
                       train_kwargs.get("hidden", 64))
    if path.exists():
        try:
            brain = ToolBrain.load(path)
            if brain.meta.get("fingerprint") == want:
                return brain
        except (OSError, ValueError, KeyError):
            pass  # a damaged or old file is simply rebuilt
    brain = train_tool_brain(declarations, held_out=held_out, **train_kwargs)
    if train_kwargs.get("cancel") is None or not train_kwargs["cancel"]():
        brain.save(path)
    return brain


FAST_LEVELS = ("read", "safe")  # the tool brain acts alone only on lookups and game/app moves that need no "yes"


class FastRouter:
    """Lets the tool brain handle short, clear commands by itself so Jarvis acts at once; the big brain then
    only puts the result into words. It stays out of anything it isn't sure of, anything long, anything that
    needs the author's yes, and anything while a yes/no question is open."""

    def __init__(self, folder: Path, min_confidence: float = 0.9, max_words: int = 25,
                 levels: tuple = FAST_LEVELS):
        self.folder = Path(folder)
        self.min_confidence, self.max_words, self.levels = min_confidence, max_words, tuple(levels)
        self.brain: Optional[ToolBrain] = None
        self.enabled = True
        self.error = ""
        self.building = False

    def build(self, declarations: list[dict], held_out: Optional[list[ToolCase]] = None, **train_kwargs) -> None:
        self.building, self.error = True, ""
        try:
            self.brain = ensure_tool_brain(self.folder, declarations, held_out=held_out, **train_kwargs)
        except Exception as exc:  # the app works without it; the big brain picks every tool instead
            self.error = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            self.building = False

    def engine(self, min_confidence: Optional[float] = None) -> Optional[ToolBrainEngine]:
        if self.brain is None:
            return None
        return ToolBrainEngine(self.brain, self.min_confidence if min_confidence is None else min_confidence)

    def route(self, text: str, declarations: list[dict], level_of: Callable[[str], str]) -> Optional[dict]:
        text = (text or "").strip()
        if not self.enabled or self.brain is None or not text or len(text.split()) > self.max_words:
            return None
        resp = self.engine().generate(LLMRequest(role="chat", tools=declarations,
                                                 history=[{"role": "user", "parts": [{"text": text}]}]))
        if not resp.function_calls:
            return None
        call = resp.function_calls[0]
        if level_of(call["name"]) not in self.levels:
            return None
        return {"name": call["name"], "args": dict(call["args"]), "confidence": resp.data["confidence"]}

    def status(self) -> dict:
        if self.brain is None:
            return {"ready": False, "building": self.building, "problem": self.error}
        return {"ready": True, "on": self.enabled, "tools": len(self.brain.tools) - 1,
                "trained_on": self.brain.meta.get("examples"), "acts_alone_on": list(self.levels),
                "min_confidence": self.min_confidence}
