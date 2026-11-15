"""Against the real Ollama + LFM3 on this computer. Runs only with JB_REAL_LFM3=1 (it needs the 1 GB model)."""
import os

import pytest

from jarvis_books.llm.local import LocalEngine, OllamaClient
from jarvis_books.llm.models import LLMRequest

pytestmark = pytest.mark.skipif(os.environ.get("JB_REAL_LFM3") != "1", reason="set JB_REAL_LFM3=1 to run")


@pytest.fixture(scope="module")
def eng():
    engine = LocalEngine(OllamaClient())
    if not engine.initialize():
        assert engine.download(), engine.problem
    return engine


def test_real_answer(eng):
    r = eng.generate(LLMRequest(role="chat", system="You are Jarvis.", instruction="Say hello in five words."))
    assert r.text and "<think>" not in r.text


def test_real_tool_call(eng):
    tools = [{"name": "open_book", "description": "Open one of the author's books by title",
              "parameters": {"type": "OBJECT", "properties": {"book": {"type": "STRING"}}, "required": ["book"]}}]
    r = eng.generate(LLMRequest(role="chat", system="Use tools when they help.", instruction="Open my book Glass City.",
                                tools=tools))
    assert r.function_calls and r.function_calls[0]["name"] == "open_book"


def test_real_structured_answer(eng):
    schema = {"type": "OBJECT", "properties": {"title": {"type": "STRING"}, "chapters": {"type": "INTEGER"}},
              "required": ["title", "chapters"]}
    r = eng.generate(LLMRequest(role="planner", instruction="Title and chapter count for a mystery.", json_schema=schema))
    assert isinstance(r.data["title"], str) and isinstance(r.data["chapters"], int)
