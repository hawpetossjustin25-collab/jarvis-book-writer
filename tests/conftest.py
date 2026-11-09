import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeGemini  # noqa: E402
from jarvis_books.llm.engine import AdaptiveLLMEngine  # noqa: E402
from jarvis_books.llm.models import HardwareProfile  # noqa: E402
from jarvis_books.llm.store import EngineStore  # noqa: E402
from jarvis_books.storage.db import Database  # noqa: E402
from jarvis_books.storage.library import Library  # noqa: E402


@pytest.fixture
def library(tmp_path):
    db = Database(tmp_path / "library.db")
    yield Library(db)
    db.close()


def hardware():
    return HardwareProfile(total_ram_bytes=16 * 1024 ** 3, available_ram_bytes=6 * 1024 ** 3, cpu_count=2,
                           gpu_available=False, gpu_name=None, disk_free_bytes=10 ** 12, audio_inputs=["mic"],
                           audio_outputs=["spk"], network_latency_ms=30.0, detection_warnings=[], sources={},
                           measured_at=0.0)


@pytest.fixture
def fake():
    return FakeGemini()


class FakeClock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def make_engine(tmp_path, clock):
    def build(client, probe=True, **kw):
        engine = AdaptiveLLMEngine(client, EngineStore(tmp_path / "engine.db"), hardware_probe=hardware,
                                   sleep=clock.sleep, clock=clock, **kw)
        engine.initialize(probe=probe)
        return engine
    return build


@pytest.fixture
def engine(make_engine, fake):
    return make_engine(fake)
