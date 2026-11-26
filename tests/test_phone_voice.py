"""The phone app's hands-free talk, driven in a real browser: listen, send, read the reply in Jarvis's voice, then
listen again; spoken Yes/No answers the gate; "stop listening" ends it; the phone's own voice is the backup.

The phone's microphone and speaker are stood in for: a fake SpeechRecognition hears what the test says, and audio
"plays" instantly. Everything else (pairing, the API, the app's code) is the real thing, served through the
Cloudflare address the app expects."""
from __future__ import annotations

import time

import pytest

from jarvis_books.assistant.permissions import PermissionGate
from jarvis_books.phone import PhoneServer
from test_phone import FakeAssistant, FakeVoice
from test_studio_upload import browser_launch

HOST = "calm-river-blue-sky.trycloudflare.com"

FAKE_PHONE = """
window.__recs = [];
window.__spoken = [];
window.__played = 0;
class FakeRecognition {
  constructor() { this.started = false; this.ended = false; window.__recs.push(this); }
  start() { this.started = true; }
  stop() { this.end(); }
  abort() { this.end(); }
  end() { if (this.ended) return; this.ended = true; this.started = false; if (this.onend) this.onend(); }
  say(words) {
    const result = [{transcript: words}];
    result.isFinal = true;
    this.onresult({results: [result]});
    this.end();
  }
  silence() { if (this.onerror) this.onerror({error: 'no-speech'}); this.end(); }
  deny() { if (this.onerror) this.onerror({error: 'not-allowed'}); this.end(); }
}
window.SpeechRecognition = FakeRecognition;
HTMLMediaElement.prototype.play = function () {
  window.__played += 1;
  setTimeout(() => this.dispatchEvent(new Event('ended')), 50);
  return Promise.resolve();
};
HTMLMediaElement.prototype.pause = function () {};
speechSynthesis.speak = (u) => { window.__spoken.push(u.text); setTimeout(() => u.onend && u.onend(), 50); };
"""


class Turns(FakeAssistant):
    """Answers each message with the next reply; a reply of None asks the gate first."""

    def __init__(self, gate, replies):
        super().__init__()
        self.gate, self.replies, self.ran = gate, list(replies), []

    def send(self, text, on_tool=None, on_text=None, decided=None):
        self.calls.append((text, decided))
        if decided is not None:
            return "Done." if decided.get("approved", True) else "Okay, I won't."
        reply = self.replies.pop(0)
        if reply is None:
            self.gate.request("run_command", {"command": "dir"}, "run dir", lambda: self.ran.append(1) or {"ok": 1},
                              source="phone")
            return "Shall I run dir?"
        return reply


@pytest.fixture
def phone(library):
    made = []

    def build(replies, voice):
        gate = PermissionGate(library)
        assistant = Turns(gate, replies)
        srv = PhoneServer(library, assistant, gate, port=0, voice=voice)
        assert srv.start()
        made.append(srv)
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True, **browser_launch())
        ctx = browser.new_context(service_workers="block", viewport={"width": 390, "height": 800})
        ctx.add_init_script(FAKE_PHONE)
        local = f"http://127.0.0.1:{srv.port}"
        ctx.route(f"https://{HOST}/**", lambda route: route.fulfill(
            response=route.fetch(url=route.request.url.replace(f"https://{HOST}", local))))
        page = ctx.new_page()
        page.goto(f"https://{HOST}/#c={srv.pair_code()}")
        page.wait_for_selector("#chat:not([hidden])")
        made.append((pw, browser))
        return page, assistant

    yield build
    for item in made:
        if isinstance(item, tuple):
            item[1].close()
            item[0].stop()
        else:
            item.stop()


def wait_for(page, js, timeout=8.0):
    page.wait_for_function(f"() => {js}", timeout=timeout * 1000)  # a function: the app's CSP forbids eval


def listening(page, n):
    """The n-th microphone session has started and is the newest one."""
    wait_for(page, f"window.__recs.length === {n} && window.__recs[{n - 1}].started")


def until(page, check, timeout=8.0):
    """Waits on the Python side. Playwright only passes the app's requests on to Jarvis while it is called, so the
    wait goes through the page instead of time.sleep."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check():
            return
        page.wait_for_timeout(50)
    raise AssertionError("it never happened")


def test_hands_free_listens_answers_out_loud_and_listens_again(phone):
    voice = FakeVoice()
    page, jarvis = phone(["It's 9 PM.", None], voice)
    assert page.is_visible("#handsFree")
    page.click("#handsFree")
    assert page.get_attribute("#handsFree", "aria-pressed") == "true"
    listening(page, 1)
    page.evaluate("window.__recs[0].say('what time is it')")
    until(page, lambda: jarvis.calls[:1] == [("what time is it", None)])
    wait_for(page, "window.__played === 1")  # read in Jarvis's Settings voice, not the phone's
    listening(page, 2)  # ...and then he listens again by himself
    assert voice.said == [("It's 9 PM.", False)] and page.evaluate("window.__spoken") == []
    assert page.inner_text("#log").count("It's 9 PM.") == 1

    page.evaluate("window.__recs[1].silence()")  # nothing said: keep listening
    listening(page, 3)

    page.evaluate("window.__recs[2].say('run dir')")
    page.wait_for_selector(".ask button")
    listening(page, 4)
    page.evaluate("window.__recs[3].say('yes please')")  # a spoken yes answers the gate
    until(page, lambda: jarvis.ran == [1] and jarvis.calls[-1][0] == "yes")
    page.wait_for_selector(".ask button", state="detached")
    listening(page, 5)

    page.evaluate("window.__recs[4].say('stop listening')")
    wait_for(page, "document.getElementById('handsFree').getAttribute('aria-pressed') === 'false'")
    page.wait_for_timeout(800)
    assert page.evaluate("window.__recs.length") == 5 and len(jarvis.calls) == 3  # off: no more listening


def test_the_phone_voice_takes_over_and_a_tap_interrupts(phone):
    page, jarvis = phone(["Hello Justin.", "Second answer."], FakeVoice(fail=True))
    page.evaluate("""() => {
      HTMLMediaElement.prototype.play = function () { window.__played += 1; return Promise.resolve(); };
      speechSynthesis.speak = (u) => { window.__spoken.push(u.text); };  // never finishes by itself
    }""")
    page.click("#handsFree")
    listening(page, 1)
    page.evaluate("window.__recs[0].say('hello')")
    wait_for(page, "window.__spoken.length === 1")  # Jarvis's voice is out, so the phone reads it
    assert page.evaluate("window.__spoken") == ["Hello Justin."] and page.inner_text("#mic") == "Interrupt"
    page.click("#mic")  # interrupt him mid-sentence
    listening(page, 2)
    assert page.inner_text("#mic") == "Stop"
    page.evaluate("window.__recs[1].deny()")  # the microphone is blocked: hands-free stops and says why
    wait_for(page, "document.getElementById('handsFree').getAttribute('aria-pressed') === 'false'")
    assert "allow the microphone" in page.inner_text("#banner")
    page.fill("#text", "typed message")
    page.click("#send")
    until(page, lambda: jarvis.calls[-1][0] == "typed message")
    wait_for(page, "document.getElementById('log').innerText.includes('Second answer.')")
    page.wait_for_timeout(500)
    assert page.evaluate("window.__spoken") == ["Hello Justin."]  # Voice off and hands-free off: typed replies stay quiet
    assert page.evaluate("window.__recs.length") == 2
