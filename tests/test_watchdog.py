import json
import threading

from jarvis_books.watchdog import FreezeWatchdog, thread_stacks


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(tmp_path, clock, **kw):
    fired = []
    dog = FreezeWatchdog(lambda: fired.append(clock()), history=tmp_path / "restarts.json", clock=clock,
                         wall=clock, dump_after_s=20, restart_after_s=90, check_s=2, **kw)
    return dog, fired


def run(dog, clock, seconds, beat=False):
    """Advance time in check-sized steps, as the watchdog thread would."""
    out = []
    for _ in range(int(seconds / 2)):
        clock.t += 2
        if beat:
            dog.beat()
        out.append(dog.check())
    return out


def test_a_window_that_keeps_beating_is_left_alone(tmp_path):
    clock = Clock()
    dog, fired = make(tmp_path, clock)
    assert set(run(dog, clock, 600, beat=True)) == {None} and not fired


def test_a_stuck_window_gets_its_threads_logged_then_one_restart(tmp_path, caplog):
    clock = Clock()
    dog, fired = make(tmp_path, clock)
    results = run(dog, clock, 200)
    assert results.index("dump") == 9  # 20 s without a beat
    assert results.count("dump") == 1 and results.count("restart") == 1
    assert results.index("restart") == 44 and fired == [1090.0]
    assert "has not responded" in caplog.text and "--- thread" in caplog.text


def test_a_short_hiccup_only_logs_and_the_next_one_logs_again(tmp_path):
    clock = Clock()
    dog, fired = make(tmp_path, clock)
    assert "dump" in run(dog, clock, 30)
    run(dog, clock, 10, beat=True)
    assert "dump" in run(dog, clock, 30) and not fired


def test_a_sleeping_computer_is_not_a_frozen_window(tmp_path):
    clock = Clock()
    dog, fired = make(tmp_path, clock)
    clock.t += 3 * 3600  # lid closed: neither the window nor the watchdog ran
    assert dog.check() is None
    assert set(run(dog, clock, 10)) == {None} and not fired


def test_restarts_are_capped_so_a_freeze_on_every_start_cannot_loop(tmp_path):
    clock = Clock()
    for n in range(3):
        dog, fired = make(tmp_path, clock)
        assert "restart" in run(dog, clock, 100) and fired
    dog, fired = make(tmp_path, clock)
    assert "gave_up" in run(dog, clock, 100) and not fired
    assert len(json.loads((tmp_path / "restarts.json").read_text())) == 3
    clock.t += 1801  # half an hour later it may restart again
    dog, fired = make(tmp_path, clock)
    assert "restart" in run(dog, clock, 100) and fired


def test_a_stopped_watchdog_never_restarts_a_closing_window(tmp_path):
    clock = Clock()
    dog, fired = make(tmp_path, clock)
    dog.stop()
    assert set(run(dog, clock, 200)) == {None} and not fired


def test_the_watchdog_thread_fires_on_a_real_stuck_beat():
    hit = threading.Event()
    dog = FreezeWatchdog(hit.set, dump_after_s=0.1, restart_after_s=0.2, check_s=0.05)
    dog.start()
    try:
        assert hit.wait(5)
    finally:
        dog.stop()


def test_thread_stacks_names_every_thread():
    stop = threading.Event()
    t = threading.Thread(target=stop.wait, name="busy-upgrade", daemon=True)
    t.start()
    try:
        text = thread_stacks()
        assert "--- thread busy-upgrade" in text and "--- thread MainThread" in text
    finally:
        stop.set()
