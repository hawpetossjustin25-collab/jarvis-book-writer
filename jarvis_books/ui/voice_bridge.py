"""Connects the laptop microphone and speakers (Qt Multimedia) to a Gemini Live session,
with an optional on-device "Hey Jarvis" wake word."""
from __future__ import annotations

import logging
import time
from typing import Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtMultimedia import QAudioFormat, QAudioSink, QAudioSource, QMediaDevices

from jarvis_books.voice.duplex import DuplexGate, EchoCanceller
from jarvis_books.voice.live import IN_RATE, OUT_RATE, LiveSession

log = logging.getLogger(__name__)


def _format(rate: int) -> QAudioFormat:
    fmt = QAudioFormat()
    fmt.setSampleRate(rate)
    fmt.setChannelCount(1)
    fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
    return fmt


def _onnx_runtime_dlls() -> None:
    """onnxruntime needs the MSVC C++ runtime. PyQt6 ships those DLLs, so laptops without the
    Visual C++ redistributable can still load it (no admin install needed)."""
    import os
    import sys
    if sys.platform != "win32":
        return
    import PyQt6
    qt_bin = os.path.join(os.path.dirname(PyQt6.__file__), "Qt6", "bin")
    if os.path.isdir(qt_bin):
        os.add_dll_directory(qt_bin)


def ensure_wake_model() -> str:
    """Make sure the 'hey_jarvis' model files are present (downloads them once)."""
    import os
    _onnx_runtime_dlls()
    import openwakeword
    import openwakeword.utils
    folder = os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models")
    if not os.path.exists(os.path.join(folder, "hey_jarvis_v0.1.onnx")):
        openwakeword.utils.download_models(model_names=["hey_jarvis"])
    return folder


class WakeWord:
    """openWakeWord 'hey_jarvis' detector (runs locally, nothing leaves the laptop)."""

    FRAME = 1280  # 80 ms at 16 kHz

    def __init__(self, threshold: float = 0.5):
        import numpy as np
        ensure_wake_model()
        from openwakeword.model import Model
        self._np = np
        try:
            self.model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
        except TypeError:
            self.model = Model(wakeword_models=["hey_jarvis"])
        self.threshold = threshold
        self._buf = bytearray()

    def feed(self, pcm: bytes) -> bool:
        self._buf.extend(pcm)
        hit = False
        while len(self._buf) >= self.FRAME * 2:
            frame = self._np.frombuffer(bytes(self._buf[:self.FRAME * 2]), dtype=self._np.int16)
            del self._buf[:self.FRAME * 2]
            scores = self.model.predict(frame)
            if any(v >= self.threshold for v in scores.values()):
                hit = True
        if hit:
            self.model.reset()
            self._buf.clear()
        return hit


class VoiceBridge(QObject):
    state = pyqtSignal(str)
    transcript = pyqtSignal(str, str)
    tool_used = pyqtSignal(str, str)
    level = pyqtSignal(float)
    external_barge = pyqtSignal()  # the user talked over the audiobook: the window pauses it
    _audio_in = pyqtSignal(bytes)
    _interrupt = pyqtSignal()
    _state_in = pyqtSignal(str)

    def __init__(self, make_session, parent=None):
        super().__init__(parent)
        self.make_session = make_session  # callable(**callbacks) -> LiveSession
        self.session: Optional[LiveSession] = None
        self.source: Optional[QAudioSource] = None
        self.mic_io = None
        self.sink: Optional[QAudioSink] = None
        self.sink_io = None
        self._out = bytearray()
        self.wake: Optional[WakeWord] = None
        self.wake_error = ""
        self.muted = False
        self.external_playing = lambda: False  # other app audio (the audiobook) is playing out loud
        self.gate = DuplexGate(IN_RATE)
        self.aec = EchoCanceller.create(IN_RATE, OUT_RATE)
        self._turn_open = False  # Jarvis audio for the current answer is still arriving
        self._drop_turn = False  # the user barged in: discard the rest of the answer Jarvis was giving
        self._audio_in.connect(self._queue_audio)
        self._interrupt.connect(self._on_interrupted)
        self._state_in.connect(self._on_session_state)
        self._pump = QTimer(self)
        self._pump.setInterval(20)
        self._pump.timeout.connect(self._pump_audio)

    # ---- devices -------------------------------------------------------------------
    def _open_devices(self) -> None:
        if self.source is None:
            device = QMediaDevices.defaultAudioInput()
            if device.isNull():
                raise RuntimeError("No microphone found")
            self.source = QAudioSource(device, _format(IN_RATE), self)
            self.source.setBufferSize(IN_RATE // 5)
            self.mic_io = self.source.start()
            self.mic_io.readyRead.connect(self._on_mic)
        if self.sink is None:
            out = QMediaDevices.defaultAudioOutput()
            self.sink = QAudioSink(out, _format(OUT_RATE), self)
            self.sink.setBufferSize(OUT_RATE)  # ~0.5 s
            self.sink_io = self.sink.start()
            self._pump.start()

    def _close_devices(self) -> None:
        self._pump.stop()
        for dev in (self.source, self.sink):
            if dev is not None:
                dev.stop()
        self.source = self.sink = None
        self.mic_io = self.sink_io = None
        self._out.clear()

    # ---- wake word -----------------------------------------------------------------
    def enable_wake_word(self, on: bool) -> bool:
        if not on:
            self.wake = None
            if self.session is None:
                self._close_devices()
            return False
        try:
            self.wake = WakeWord()
            self._open_devices()
            self.state.emit("waiting for 'Hey Jarvis'")
            return True
        except Exception as exc:
            self.wake = None
            self.wake_error = str(exc)
            log.warning("wake word unavailable: %s", exc)
            return False

    # ---- session ---------------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self.session is not None

    def start(self) -> None:
        if self.session is not None:
            return
        self._open_devices()
        self.session = self.make_session(
            on_audio=self._audio_in.emit, on_interrupted=self._interrupt.emit,
            on_transcript=lambda role, text: self.transcript.emit(role, text),
            on_state=self._state_in.emit,
            on_tool=lambda name, args, result: self.tool_used.emit(name, str(result)[:300]))
        if not self.session.start(wait_s=20):
            self.session.stop()
            self.session = None
            raise RuntimeError("Could not connect to Gemini Live")

    def stop(self) -> None:
        if self.session is not None:
            self.session.stop()
            self.session = None
        self._out.clear()
        if self.wake is None:
            self._close_devices()
        else:
            self.state.emit("waiting for 'Hey Jarvis'")

    def send_text(self, text: str) -> None:
        if self.session is not None:
            self.session.send_text(text)

    @property
    def auto_mute(self) -> bool:
        return self.gate.enabled

    @auto_mute.setter
    def auto_mute(self, on: bool) -> None:
        self.gate.enabled = bool(on)

    def _is_playing(self) -> bool:
        if self._out:
            return True
        return self.sink is not None and self.sink.bufferSize() - self.sink.bytesFree() > 0

    def _barge_in(self) -> None:
        """The user talked over Jarvis: stop him now and drop the rest of that answer."""
        self._drop_turn = self._turn_open
        self._clear_audio()
        self.state.emit("listening")

    def _on_interrupted(self) -> None:
        self._drop_turn = self._turn_open = False
        self._clear_audio()

    def _on_session_state(self, state: str) -> None:
        if state == "listening":  # Gemini finished (or abandoned) its turn
            self._drop_turn = self._turn_open = False
        elif state == "speaking" and self.gate.enabled:
            state = "speaking (talk over me to interrupt)"
        self.state.emit(state)

    def _on_mic(self) -> None:
        if self.mic_io is None:
            return
        data = bytes(self.mic_io.readAll())
        if not data:
            return
        if self.aec is not None and self.session is not None:
            queued = self.sink.bufferSize() - self.sink.bytesFree() if self.sink is not None else 0
            data = self.aec.process(data, delay_ms=queued // (OUT_RATE * 2 // 1000) + 30)
            if not data:
                return
        if len(data) >= 2:
            import array
            samples = array.array("h", data[: len(data) - len(data) % 2])
            peak = max((abs(s) for s in samples[::8]), default=0)
            self.level.emit(min(1.0, peak / 12000))
        if self.session is not None:
            self._route_mic(data, time.monotonic())
        elif self.wake is not None and self.wake.feed(data):
            try:
                self.start()
                self.session.send_text("(The user just said 'Hey Jarvis'. Greet them briefly.)")
            except Exception as exc:
                self.state.emit(f"error: {exc}")

    def _route_mic(self, data: bytes, now: float) -> None:
        """Send the mic to Gemini, holding it back while Jarvis or the audiobook is playing out loud."""
        jarvis = self._is_playing()
        book = not jarvis and bool(self.external_playing())
        self.gate.set_playing(jarvis or book, now)
        if self.muted:
            return
        out, barged = self.gate.feed(data)
        if barged:
            if book:
                self.external_barge.emit()
                self.state.emit("listening")
            else:
                self._barge_in()
        if out:
            self.session.send_audio(out)

    def _queue_audio(self, pcm: bytes) -> None:
        if self._drop_turn:
            return
        self._turn_open = True
        self._out.extend(pcm)

    def _clear_audio(self) -> None:  # barge-in: the user started talking over Jarvis
        self._out.clear()
        if self.sink is not None:
            self.sink.reset()
            self.sink_io = self.sink.start()

    def _pump_audio(self) -> None:
        if self.sink is None or self.sink_io is None or not self._out:
            return
        free = self.sink.bytesFree()
        if free <= 0:
            return
        n = min(free - free % 2, len(self._out))
        if n > 0:
            written = self.sink_io.write(bytes(self._out[:n]))
            if written and written > 0:
                chunk = bytes(self._out[:written])
                self.gate.played(chunk)
                if self.aec is not None:
                    self.aec.played(chunk)
                del self._out[:written]
