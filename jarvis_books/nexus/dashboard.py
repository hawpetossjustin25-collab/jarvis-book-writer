"""The NEXUS dashboard: one snapshot of every NEXUS system, read from the systems themselves (their SQLite stores
and live objects). Nothing is estimated: a section that can't be read says why instead of showing numbers."""
from __future__ import annotations

import html
import logging
import time
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)
SECTIONS = ("api", "search", "crawl", "facts", "tools", "collective", "models", "improvement", "releases")


def _read(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as exc:  # one broken system must not blank the whole dashboard
        log.warning("nexus dashboard: %s", exc)
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}


def snapshot(studio, now: Optional[float] = None) -> dict:
    """Every section's current state. `studio` needs the NEXUS systems Studio wires up; missing ones are skipped."""
    now = time.time() if now is None else now
    os1 = getattr(studio, "nexus", None)
    out: dict = {"taken_at": now}
    api, hub = getattr(studio, "nexus_api", None), getattr(studio, "nexus_hub", None)
    if api is not None and hub is not None:
        out["api"] = _read(lambda: {**api.status(), **hub.health()})
    if os1 is not None:
        out["search"] = _read(lambda: {**os1.store.search_stats(),
                                       "ranking": os1.search_engine.weights.to_dict()})
        out["crawl"] = _read(lambda: {**os1.store.crawl_stats(), "crawl_running": os1._crawl_lock.locked()})
        out["improvement"] = _read(lambda: {"status": os1.improver.status(),
                                            "history": os1.improver.history(limit=8)})
    os2 = getattr(studio, "nexus_os2", None)
    if os2 is not None:
        out["facts"] = _read(lambda: os2._op_status({})[0][0])
    os3 = getattr(studio, "nexus_os3", None)
    if os3 is not None:
        out["tools"] = _read(lambda: {"registered": len(os3.registry.names()), "recent": os3.history(10)})
    col = getattr(studio, "nexus_collective", None)
    if col is not None:
        out["collective"] = _read(lambda: {**col.coordinator.status(), "worker_list": [
            {"name": w["name"], "status": w["status"], "healthy": w["healthy"], "local": w["local"],
             "memory_gb": w["memory_gb"], "last_seen": w["last_seen"],
             "cpu_percent": (w["telemetry"] or {}).get("cpu_percent")}
            for w in col.coordinator.workers(include_revoked=False)]})
    hub_models = getattr(studio, "nexus_models", None)
    if hub_models is not None:
        out["models"] = _read(lambda: {"installed": hub_models.models(), "installs": hub_models.installs(5)})
    releases = getattr(studio, "releases", None)
    if releases is not None:
        out["releases"] = _read(releases.center)
    return out


# ---- HTML for the Jarvis tab

def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _when(ts: Optional[float], now: float) -> str:
    if not ts:
        return "never"
    gap = now - ts
    if gap < 0:
        return time.strftime("%b %d %I:%M %p", time.localtime(ts))
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if gap >= size:
            n = int(gap // size)
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"


def _table(rows: list[tuple], head: Optional[tuple] = None) -> str:
    cells = "".join("<tr>" + "".join(f"<td>{_e(c)}</td>" for c in r) + "</tr>" for r in rows)
    top = "<tr>" + "".join(f"<th align='left'>{_e(h)}</th>" for h in head) + "</tr>" if head else ""
    return f"<table cellspacing='0' cellpadding='3'>{top}{cells}</table>"


def _section(title: str, body: str) -> str:
    return f"<h3>{_e(title)}</h3>{body}"


def _problem(data: Any) -> Optional[str]:
    if isinstance(data, dict) and set(data) == {"error"}:
        return f"<p><b>Couldn't read this:</b> {_e(data['error'])}</p>"
    return None


def render(snap: dict) -> str:
    """The snapshot as HTML for a QTextBrowser."""
    now = snap.get("taken_at") or time.time()
    parts = [f"<p><i>Updated {time.strftime('%I:%M:%S %p', time.localtime(now))}</i></p>"]

    def add(key: str, title: str, build: Callable[[dict], str]) -> None:
        if key not in snap:
            return
        data = snap[key]
        parts.append(_section(title, _problem(data) or _read_html(build, data)))

    def api(d):
        state = f"running at {d['url']}" if d.get("running") else f"stopped {d.get('error') or ''}".strip()
        rows = [(name, s["requests"], s["errors"], s["timeouts"], f"{s['avg_ms']:.1f} ms")
                for name, s in sorted((d.get("systems") or {}).items())]
        return (f"<p>NEXUS API {_e(state)}, up {_e(round((d.get('uptime_s') or 0) / 60))} minutes.</p>" +
                _table(rows, ("System", "Requests", "Errors", "Timeouts", "Average")))

    def search(d):
        speed = (f"median {d['median_ms']} ms, slowest 5% {d['p95_ms']} ms" if d.get("median_ms") is not None
                 else "no searches yet")
        return _table([("Searchable pages", d["searchable"]), ("Pages with a published date", d["dated_pages"]),
                       ("Published in the last year", d["published_last_year"]),
                       ("Checked in the last week", d["checked_last_week"]),
                       ("Pages with meaning vectors", d["vectors"]), ("Searches so far", d["searches"]),
                       ("Search speed (last 200)", speed),
                       ("Answered from cache", _pct(d.get("cached_share"))),
                       ("Searches with no results", _pct(d.get("empty_share")))])

    def crawl(d):
        runs = [(_when(r["started_at"], now), r["status"], r["topic"] or "-",
                 (r["stats"] or {}).get("pages_fetched", "-")) for r in d.get("recent_runs", [])]
        status = ", ".join(f"{k} {v}" for k, v in sorted(d.get("by_status", {}).items()))
        return (f"<p>{'Exploring now. ' if d.get('crawl_running') else ''}{_e(d['pages'])} pages known "
                f"({_e(status)}); {_e(d['queued'])} waiting; {_e(d['due_for_revisit'])} due for a re-check; "
                f"{_e(d['visits_last_week'])} visits this week, {_e(d['failed_visits_last_week'])} failed.</p>" +
                (_table(runs, ("Started", "Status", "Topic", "Pages")) if runs else ""))

    def facts(d):
        return _table([("Pages read", d["pages_read"]), ("Waiting to be read", d["pages_waiting"]),
                       ("Facts", d["facts"]), ("Names and places", d["entities"]), ("Links between them",
                                                                                    d["relations"])])

    def tools(d):
        rows = [(_when(r.get("started_at"), now), r.get("tool_id") or r.get("name"), r.get("status"),
                 f"{(r.get('elapsed_ms') or 0):.0f} ms") for r in d.get("recent", [])]
        return (f"<p>{_e(d['registered'])} tools registered.</p>" +
                (_table(rows, ("When", "Tool", "Result", "Time")) if rows else "<p>No tool runs yet.</p>"))

    def collective(d):
        tasks = ", ".join(f"{k} {v}" for k, v in d.get("tasks", {}).items() if v) or "none yet"
        rows = [(w["name"], w["status"], "yes" if w["healthy"] else "no", "this computer" if w["local"] else
                 "joined", f"{w['memory_gb']} GB", _pct(None if w["cpu_percent"] is None else
                                                      w["cpu_percent"] / 100), _when(w["last_seen"], now))
                for w in d.get("worker_list", [])]
        return (f"<p>{_e(d['healthy_workers'])} of {_e(d['workers'])} workers answering; tasks: {_e(tasks)}.</p>" +
                (_table(rows, ("Worker", "Status", "Answering", "Where", "Memory offered", "Processor",
                               "Last heard")) if rows else "<p>No workers have joined.</p>"))

    def models(d):
        rows = [(m["name"], m.get("parameters") or "-", m.get("quantization") or "-", f"{m['size_gb']} GB",
                 {None: "not tested", True: "passed", False: "failed"}[m.get("first_answer_ok")],
                 f"{m['output_tps']:.1f}" if m.get("output_tps") else "not measured")
                for m in d.get("installed", [])]
        return (_table(rows, ("Model", "Size", "Packing", "On disk", "First answer", "Tokens/s (measured)"))
                if rows else "<p>No local models found yet (is Ollama running?).</p>")

    def improvement(d):
        st = d.get("status", {})
        lines = [f"{kind}: {'tuned' if s.get('champion') != 'built-in' else 'built-in settings'}, "
                 f"{s.get('versions', 0)} tries{', one on trial' if s.get('trial') else ''}"
                 for kind, s in st.items()]
        rows = [(_when(h.get("created_at"), now), h.get("kind"), h.get("status"),
                 (h.get("reason") or "")[:120]) for h in d.get("history", [])]
        return ("<p>" + _e("; ".join(lines)) + "</p>" +
                (_table(rows, ("When", "Part", "Result", "Why")) if rows else ""))

    def releases(d):
        run = d.get("running") or {}
        cyc = d.get("cycle") or {}
        nxt = d.get("next_major_release") or {}
        waiting = ", ".join(i["title"] for i in d.get("waiting_for_approval", [])) or "nothing"
        return _table([("Running version", f"{run.get('version', '?')} ({str(run.get('commit') or '?')[:7]}, "
                                           f"{run.get('channel', '?')})"),
                       ("This cycle", f"{cyc.get('name', '-')}, {cyc.get('days_left', '-')} days left"),
                       ("Next major release", f"{nxt.get('version', '-')}" + (
                           f" around {time.strftime('%b %d %Y', time.localtime(nxt['target']))}"
                           if nxt.get("target") else "")),
                       ("Ideas proposed", d.get("proposed", 0)), ("Waiting for your yes", waiting),
                       ("Rollbacks", len(d.get("rollbacks") or []))])

    add("api", "NEXUS API", api)
    add("search", "Search (OS1)", search)
    add("crawl", "Web exploring (OS1)", crawl)
    add("facts", "Facts (OS2)", facts)
    add("tools", "Tools (OS3)", tools)
    add("collective", "Collective", collective)
    add("models", "Models on this computer", models)
    add("improvement", "Self-improvement", improvement)
    add("releases", "Release Center", releases)
    return "".join(parts)


def _pct(share: Optional[float]) -> str:
    return "-" if share is None else f"{share * 100:.0f}%"


def _read_html(build: Callable[[dict], str], data: dict) -> str:
    try:
        return build(data)
    except Exception as exc:
        log.warning("nexus dashboard render: %s", exc)
        return f"<p><b>Couldn't show this:</b> {_e(type(exc).__name__)}: {_e(exc)}</p>"
