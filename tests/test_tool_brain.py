"""The tool brain: training data matches the real tools, and a trained brain picks the tool and copies the
details exactly, says nothing when unsure, and only offers calls that pass the tool's own schema."""
import random

import pytest

from jarvis_books.assistant.tools import DECLARATIONS
from jarvis_books.llm.models import LLMRequest
from jarvis_books.llm.schema import validate
from jarvis_books.llm.tool_brain import (MODEL_NAME, ToolBrain, ToolBrainEngine, ensure_tool_brain, fingerprint,
                                         tokenize, train_tool_brain)
from jarvis_books.llm.tool_data import (Template, generate, load_templates, parse_number, render)
from jarvis_books.llm.tool_eval import load_cases

MINI = [
    {"name": "open_book", "description": "Open a book.",
     "parameters": {"type": "OBJECT", "properties": {"book": {"type": "STRING"}}, "required": ["book"]}},
    {"name": "game_bid", "description": "Bid.",
     "parameters": {"type": "OBJECT", "properties": {"amount": {"type": "NUMBER"}}, "required": ["amount"]}},
    {"name": "read_file", "description": "Read a file.",
     "parameters": {"type": "OBJECT", "properties": {"path": {"type": "STRING"}}, "required": ["path"]}},
    {"name": "media_control", "description": "Music.",
     "parameters": {"type": "OBJECT", "properties": {"action": {"type": "STRING", "enum": ["play_pause", "mute"]}},
                    "required": ["action"]}},
    {"name": "researcher_switch", "description": "Researcher on or off.",
     "parameters": {"type": "OBJECT", "properties": {"on": {"type": "BOOLEAN"}}, "required": ["on"]}},
]
MINI_TEMPLATES = """
open_book | open [my book|the book|] {book:book}
open_book | let's work on {book:book}
game_bid | bid {amount:money}
game_bid | [raise it to|go to] {amount:money}
read_file | read [the file|] {path:path}
read_file | open {path:path} and tell me what it says
media_control | [pause|play] the music | {"action": "play_pause"}
media_control | mute the [sound|computer] | {"action": "mute"}
researcher_switch | turn the researcher off | {"on": false}
researcher_switch | turn the researcher on | {"on": true}
(none) | {x:chat}
"""


@pytest.fixture(scope="module")
def mini(tmp_path_factory):
    path = tmp_path_factory.mktemp("tb") / "templates.txt"
    path.write_text(MINI_TEMPLATES, encoding="utf-8")
    brain = train_tool_brain(MINI, templates_path=path, per_template=60, epochs=8, hidden=32, buckets=1 << 13)
    return brain, path


def test_tokenize_keeps_paths_links_and_money_whole():
    words = [t[0] for t in tokenize(r"read C:\Users\hawpe\notes.txt, then bid $2,500 at https://x.com/a?b=1.")]
    assert r"C:\Users\hawpe\notes.txt" in words and "$2,500" in words and "https://x.com/a?b=1" in words


@pytest.mark.parametrize("text,value", [("$2,500", 2500), ("2500", 2500), ("3k", 3000), ("three", 3),
                                        ("twice", 2), ("12.5", 12.5), ("lots", None)])
def test_parse_number(text, value):
    assert parse_number(text) == value


def test_every_template_names_a_real_tool_and_every_tool_has_templates():
    names = {d["name"] for d in DECLARATIONS}
    templates = load_templates()
    assert {t.tool for t in templates if t.tool} == names


def test_generated_examples_pass_the_real_schemas_and_spans_point_at_the_values():
    by_name = {d["name"]: d for d in DECLARATIONS}
    examples = generate(load_templates(), per_template=8, held_out=load_cases())
    assert len(examples) > 1500
    for ex in examples:
        if ex.tool is None:
            assert ex.args == {} and ex.spans == {}
            continue
        props = by_name[ex.tool]["parameters"].get("properties", {})
        assert validate(ex.args, by_name[ex.tool]["parameters"]) == [], (ex.request, ex.args)
        assert set(ex.args) <= set(props), (ex.request, ex.args)
        for key, (a, b) in ex.spans.items():
            piece = ex.request[a:b]
            value = ex.args[key]
            if isinstance(value, str) and "enum" not in props[key]:
                assert piece == value, (ex.request, key)
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                assert parse_number(piece) == value, (ex.request, key)


def test_the_tool_test_set_is_never_in_the_training_data():
    cases = load_cases()
    asked = {" ".join(c.request.lower().replace(".", "").replace("?", "").split()) for c in cases}
    made = {" ".join(e.request.lower().replace(".", "").replace("?", "").split())
            for e in generate(load_templates(), held_out=cases)}
    assert not asked & made


def test_generation_is_reproducible():
    templates = load_templates()[:20]
    first = [(e.request, e.args) for e in generate(templates, per_template=5, seed=3)]
    assert first == [(e.request, e.args) for e in generate(templates, per_template=5, seed=3)]
    assert first != [(e.request, e.args) for e in generate(templates, per_template=5, seed=4)]


def test_broken_template_lines_are_reported(tmp_path):
    bad = tmp_path / "t.txt"
    bad.write_text("open_book | open {book:nopool}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown pool"):
        load_templates(bad)
    bad.write_text("just one part\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 1"):
        load_templates(bad)


def test_render_tracks_where_each_value_landed():
    ex = render(Template("open_book", "open {book:book} [now|]"), random.Random(1))
    a, b = ex.spans["book"]
    assert ex.request[a:b] == ex.args["book"]


def test_trained_brain_picks_tools_and_copies_details_it_never_saw(mini):
    brain, _ = mini
    p = brain.predict("open my book The Velvet Dynamo")
    assert p.tool == "open_book" and p.args == {"book": "The Velvet Dynamo"}
    assert brain.predict("bid $4,321").args == {"amount": 4321}
    p = brain.predict(r"read the file D:\Notes\plan_b.txt")
    assert p.tool == "read_file" and p.args == {"path": r"D:\Notes\plan_b.txt"}
    assert brain.predict("mute the computer").args == {"action": "mute"}
    assert brain.predict("turn the researcher off").args == {"on": False}
    assert brain.predict("thank you").tool is None


def test_save_and_load_give_the_same_answers(mini, tmp_path):
    brain, _ = mini
    brain.save(tmp_path / "b.npz")
    again = ToolBrain.load(tmp_path / "b.npz")
    for text in ("open my book Iron Coast", "bid 2500", "pause the music"):
        assert again.predict(text).tool == brain.predict(text).tool
        assert again.predict(text).args == brain.predict(text).args


def test_engine_answers_like_a_brain_and_reads_the_last_user_turn(mini):
    brain, _ = mini
    engine = ToolBrainEngine(brain, min_confidence=0.0)
    request = LLMRequest(role="assistant", tools=MINI, history=[
        {"role": "user", "parts": [{"text": "open my book Iron Coast"}]},
        {"role": "model", "parts": [{"text": "Opened."}]},
        {"role": "user", "parts": [{"text": "bid 3,100"}]}])
    resp = engine.generate(request)
    assert resp.model == MODEL_NAME
    assert resp.function_calls == [{"name": "game_bid", "args": {"amount": 3100}}]


def test_engine_stays_quiet_when_unsure_or_the_call_would_be_invalid(mini):
    brain, _ = mini
    sure_only = ToolBrainEngine(brain, min_confidence=1.01)
    resp = sure_only.generate(LLMRequest(role="assistant", tools=MINI,
                                         history=[{"role": "user", "parts": [{"text": "bid 2500"}]}]))
    assert resp.function_calls == [] and resp.finish_reason == "UNSURE"
    assert resp.data["guess"]["name"] == "game_bid"
    strict = [dict(MINI[1], parameters={"type": "OBJECT", "properties": {"amount": {"type": "NUMBER"},
                                                                          "lot": {"type": "STRING"}},
                                        "required": ["amount", "lot"]})]
    resp = ToolBrainEngine(brain, 0.0).generate(LLMRequest(role="assistant", tools=strict, history=[
        {"role": "user", "parts": [{"text": "bid 2500"}]}]))
    assert resp.function_calls == [] and resp.finish_reason == "UNSURE"


def test_engine_only_picks_tools_it_was_offered(mini):
    brain, _ = mini
    resp = ToolBrainEngine(brain, 0.0).generate(LLMRequest(role="assistant", tools=[MINI[2]], history=[
        {"role": "user", "parts": [{"text": "bid 2500"}]}]))
    assert all(c["name"] == "read_file" for c in resp.function_calls)


def test_ensure_reuses_a_matching_brain_and_retrains_when_tools_change(mini, tmp_path):
    _, templates = mini
    kw = dict(templates_path=templates, per_template=10, epochs=1, hidden=32, buckets=1 << 12)
    first = ensure_tool_brain(tmp_path, MINI, **kw)
    stamp = (tmp_path / "tool_brain.npz").stat().st_mtime_ns
    assert ensure_tool_brain(tmp_path, MINI, **kw).meta["fingerprint"] == first.meta["fingerprint"]
    assert (tmp_path / "tool_brain.npz").stat().st_mtime_ns == stamp
    fewer = MINI[:-1]
    again = ensure_tool_brain(tmp_path, fewer, **kw)
    assert again.meta["fingerprint"] == fingerprint(fewer, templates, 32) != first.meta["fingerprint"]
    assert "researcher_switch" not in again.tools


def test_a_damaged_brain_file_is_rebuilt(mini, tmp_path):
    _, templates = mini
    (tmp_path / "tool_brain.npz").write_bytes(b"not a model")
    brain = ensure_tool_brain(tmp_path, MINI, templates_path=templates, per_template=10, epochs=1, hidden=32,
                              buckets=1 << 12)
    assert brain.predict("bid 2500").tool == "game_bid"


MINI_LEVELS = {"open_book": "read", "game_bid": "safe", "read_file": "read", "media_control": "outside",
               "researcher_switch": "change"}


def test_router_acts_alone_only_on_clear_short_lookups_and_safe_moves(mini, tmp_path):
    from jarvis_books.llm.tool_brain import FastRouter
    brain, _ = mini
    router = FastRouter(tmp_path, min_confidence=0.5)
    assert router.route("bid 2500", MINI, MINI_LEVELS.get) is None  # not built yet
    router.brain = brain
    assert router.route("bid 2500", MINI, MINI_LEVELS.get)["args"] == {"amount": 2500}
    assert router.route("mute the computer", MINI, MINI_LEVELS.get) is None  # needs the author's yes
    assert router.route("turn the researcher off", MINI, MINI_LEVELS.get) is None
    assert router.route("bid 2500 " + "and then some more words " * 6, MINI, MINI_LEVELS.get) is None
    assert router.route("thank you", MINI, MINI_LEVELS.get) is None
    router.enabled = False
    assert router.route("bid 2500", MINI, MINI_LEVELS.get) is None
    assert router.status()["ready"] and router.status()["on"] is False


def test_router_build_problems_leave_jarvis_working(tmp_path, monkeypatch):
    from jarvis_books.llm import tool_brain
    router = tool_brain.FastRouter(tmp_path)

    def broken(*a, **k):
        raise MemoryError("out of memory")
    monkeypatch.setattr(tool_brain, "ensure_tool_brain", broken)
    router.build(MINI)
    assert router.brain is None and "MemoryError" in router.error
    assert router.route("bid 2500", MINI, MINI_LEVELS.get) is None
    assert router.status() == {"ready": False, "building": False, "problem": router.error}


def test_studio_wires_the_quick_brain_and_the_setting(tmp_path):
    from jarvis_books.studio import Studio
    studio = Studio(home=tmp_path / "home", mirror=tmp_path / "mirror")
    try:
        assert studio.assistant.fast is studio.fast is studio.tools.fast
        assert studio.tools._t_change_setting("tool_brain", "off")["value"] == "0"  # past the yes/no gate
        assert studio.fast.enabled is False
        assert "tool_brain" in studio.tools.call("brain_status", {}, source="ui")
        out = studio.tools.call("test_tool_calling", {"brain": "toolbrain"}, source="ui")
        assert "isn't ready yet" in out["error"]
    finally:
        studio.close()
