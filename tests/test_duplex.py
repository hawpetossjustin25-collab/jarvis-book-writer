import array
import math

from jarvis_books.voice.duplex import FRAME_MS, DuplexGate, rms
from jarvis_books.voice.live import LiveSession

RATE = 16000


def tone(ms, amp, freq=220):
    n = RATE * ms // 1000
    return array.array("h", (int(amp * math.sin(2 * math.pi * freq * i / RATE)) for i in range(n))).tobytes()


def tone24(ms, amp):
    n = 24000 * ms // 1000
    return array.array("h", (int(amp * math.sin(2 * math.pi * 200 * i / 24000)) for i in range(n))).tobytes()


def feed(gate, pcm, chunk_ms=40, playing_amp=0):
    step = RATE * chunk_ms // 1000 * 2
    out, barged = b"", False
    for i in range(0, len(pcm), step):
        if playing_amp:
            gate.played(tone24(chunk_ms, playing_amp))
        o, b = gate.feed(pcm[i:i + step])
        out += o
        barged = barged or b
    return out, barged


def test_mic_passes_straight_through_when_jarvis_is_quiet():
    gate = DuplexGate(RATE)
    pcm = tone(200, 3000)
    out, barged = feed(gate, pcm)
    assert out == pcm and not barged


def test_jarvis_hearing_his_own_echo_is_held_back_and_never_interrupts():
    gate = DuplexGate(RATE)
    gate.set_playing(True, 0.0)
    out, barged = feed(gate, tone(5000, 2500))  # 5 s of speaker echo at a steady level
    assert out == b"" and not barged and gate.holding
    assert 1500 < gate.echo < 2000  # learned the echo loudness (RMS of a 2500 peak sine)


def test_short_noise_does_not_interrupt_but_half_a_sentence_does():
    gate = DuplexGate(RATE)
    gate.set_playing(True, 0.0)
    feed(gate, tone(600, 2000))  # echo
    out, barged = feed(gate, tone(300, 12000) + tone(800, 2000))  # a cough
    assert not barged and out == b""
    speech = tone(900, 12000, freq=180)
    out, barged = feed(gate, speech)
    assert barged and not gate.holding
    assert len(out) >= len(speech) * 0.75  # the start of what was said is sent on, not lost
    after = tone(100, 12000)
    assert feed(gate, after)[0] == after  # mic is open again


def test_mic_reopens_after_jarvis_finishes_plus_a_short_tail():
    gate = DuplexGate(RATE, tail_ms=400)
    gate.set_playing(True, 0.0)
    feed(gate, tone(400, 2000))
    gate.set_playing(False, 1.0)
    assert gate.holding and feed(gate, tone(100, 2000))[0] == b""
    gate.set_playing(False, 1.5)
    assert not gate.holding
    pcm = tone(100, 3000)
    assert feed(gate, pcm)[0] == pcm


def test_auto_mute_off_streams_everything():
    gate = DuplexGate(RATE)
    gate.enabled = False
    gate.set_playing(True, 0.0)
    pcm = tone(300, 2000)
    assert feed(gate, pcm) == (pcm, False)


def test_rms_and_frame_size():
    assert rms(b"") == 0.0 and abs(rms(tone(FRAME_MS * 10, 1000)) - 707) < 15


def test_live_setup_uses_low_start_sensitivity():
    s = LiveSession("wss://x", {}, "m", "sys", [], lambda n, a: {})
    vad = s.setup_message()["setup"]["realtimeInputConfig"]["automaticActivityDetection"]
    assert vad["startOfSpeechSensitivity"] == "START_SENSITIVITY_LOW"


def test_loud_speakers_echo_is_learned_from_what_jarvis_plays():
    gate = DuplexGate(RATE)
    gate.set_playing(True, 0.0)
    # Speakers loud enough that the echo is far above the fixed floor (like a laptop at high volume).
    out, barged = feed(gate, tone(6000, 6000), playing_amp=12000)
    assert not barged and out == b""
    assert 0.4 < gate.coupling < 0.6 and gate.threshold() > 6000
    out, barged = feed(gate, tone(900, 25000, freq=180), playing_amp=12000)  # the user talks over him
    assert barged
