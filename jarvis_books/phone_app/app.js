'use strict';
// Jarvis phone app: pairs with the Jarvis on the laptop (through its Cloudflare link) and chats with him.
// Everything shown from Jarvis is added as text, never as HTML.

const HOST_RE = /^[a-z0-9-]+\.trycloudflare\.com$/;
const CODE_RE = /^\d{8}$/;
const URL_RE = /https?:\/\/[^\s<>"')\]]+/g;
const KEEP = 100;
const $ = (id) => document.getElementById(id);
const store = {
  get: (k) => localStorage.getItem('jarvis.' + k),
  set: (k, v) => (v == null ? localStorage.removeItem('jarvis.' + k) : localStorage.setItem('jarvis.' + k, v)),
};
let busy = false;
let chatLog = [];
let handsFree = false;  // Jarvis listens, answers out loud, then listens again, until it's switched off

function base() {
  if (HOST_RE.test(location.host)) return location.origin;  // the app is being served by Jarvis itself
  const host = store.get('server');
  return host && HOST_RE.test(host) ? 'https://' + host : null;
}

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(path, body, timeoutMs = 40000) {
  const resp = await request(path, body, timeoutMs);
  let data = {};
  try { data = await resp.json(); } catch (e) { /* not JSON: tunnel error page */ }
  if (!resp.ok) throw new ApiError(resp.status, data.error || ('error ' + resp.status));
  return data;
}

async function request(path, body, timeoutMs) {
  const root = base();
  if (!root) throw new ApiError(0, 'not connected');
  const headers = {};
  const token = store.get('token');
  if (token) headers.Authorization = 'Bearer ' + token;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  let resp;
  try {
    resp = await fetch(root + path, {method: body === undefined ? 'GET' : 'POST', headers, signal: ctl.signal,
      body: body === undefined ? undefined : JSON.stringify(body), cache: 'no-store', referrerPolicy: 'no-referrer'});
  } catch (e) {
    throw new ApiError(0, 'Jarvis is offline');
  } finally {
    clearTimeout(timer);
  }
  return resp;
}

// ---- links and setup -------------------------------------------------------------------------------------
function readLink(text) {
  // Accepts the full phone link, a #s=...&c=... fragment, or a bare trycloudflare address.
  const out = {};
  const raw = String(text || '').trim();
  const hashAt = raw.indexOf('#');
  const params = new URLSearchParams(hashAt >= 0 ? raw.slice(hashAt + 1) : '');
  let host = params.get('s');
  if (!host) {
    const m = raw.match(/([a-z0-9-]+\.trycloudflare\.com)/i);
    if (m) host = m[1];
  }
  if (host && HOST_RE.test(host.toLowerCase())) out.host = host.toLowerCase();
  const code = params.get('c');
  if (code && CODE_RE.test(code)) out.code = code;
  return out;
}

async function useLink(found) {
  if (found.host) store.set('server', found.host);
  if (found.code) return pair(found.code);
  return start();
}

async function pair(code) {
  show('pair');
  $('codeInput').value = code || '';
  $('pairError').hidden = true;
  try {
    const name = /iphone|ipad/i.test(navigator.userAgent) ? 'iPhone' : /android/i.test(navigator.userAgent) ? 'Android' : 'phone';
    const r = await api('/api/pair', {code, name});
    store.set('token', r.token);
    return start();
  } catch (e) {
    $('pairError').textContent = e.status === 403 ? 'That code is wrong or already used. Ask Jarvis for a new phone link.'
      : e.message;
    $('pairError').hidden = false;
  }
}

function show(which) {
  for (const id of ['setup', 'pair', 'chat']) $(id).hidden = id !== which;
  $('composer').hidden = which !== 'chat';
}

function setState(text, cls) {
  $('state').textContent = text;
  $('state').className = 'state ' + (cls || '');
}

function banner(text) {
  $('banner').textContent = text || '';
  $('banner').hidden = !text;
}

async function start() {
  if (!base()) { show('setup'); return; }
  if (!store.get('token')) { show('pair'); return; }
  show('chat');
  render();
  try {
    const hello = await api('/api/hello', undefined, 15000);
    if (!hello.paired) { store.set('token', null); show('pair'); return; }
    setState('online', 'ok');
    banner('');
  } catch (e) {
    setState('offline', 'bad');
    banner("Can't reach Jarvis. Make sure the laptop is on with Jarvis open. If the laptop restarted, ask Jarvis " +
           'for a new "phone link" in Telegram and open it.');
  }
}

// ---- chat ------------------------------------------------------------------------------------------------
function addText(el, text) {
  el.textContent = '';
  let last = 0;
  for (const m of text.matchAll(URL_RE)) {
    el.append(document.createTextNode(text.slice(last, m.index)));
    const a = document.createElement('a');
    a.href = m[0];
    a.textContent = m[0];
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    el.append(a);
    last = m.index + m[0].length;
  }
  el.append(document.createTextNode(text.slice(last)));
}

function bubble(who, text, wait) {
  const li = document.createElement('li');
  li.className = 'msg ' + who + (wait ? ' wait' : '');
  addText(li, text);
  $('log').append(li);
  li.scrollIntoView({block: 'end'});
  return li;
}

function render() {
  try { chatLog = JSON.parse(store.get('chat') || '[]'); } catch (e) { chatLog = []; }
  $('log').textContent = '';
  for (const m of chatLog) bubble(m.who, m.text);
}

function remember(who, text) {
  chatLog.push({who, text});
  chatLog = chatLog.slice(-KEEP);
  store.set('chat', JSON.stringify(chatLog));
}

let openAsk = null;  // {ask, row}: the Yes/No question waiting for an answer, by tap or by voice

async function answerAsk(yes) {
  if (!openAsk || busy) return;
  const {ask, row} = openAsk;
  openAsk = null;
  row.remove();
  stopListening();
  const label = yes ? 'Yes' : 'No';
  bubble('me', label);
  remember('me', label);
  try {
    const r = await api('/api/answer', {request: ask.id, yes});
    await follow(r.turn);
  } catch (e) {
    bubble('him', e.status === 409 ? 'That question is already closed.' : e.message);
    listenSoon();
  }
}

function askButtons(li, ask) {
  if (openAsk) openAsk.row.remove();
  const row = document.createElement('div');
  row.className = 'ask';
  openAsk = {ask, row};
  for (const [label, yes] of [['Yes', true], ['No', false]]) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    if (!yes) b.className = 'no';
    b.onclick = () => answerAsk(yes);
    row.append(b);
  }
  li.append(row);
  li.scrollIntoView({block: 'end'});
}

async function follow(turn) {
  busy = true;
  $('send').disabled = true;
  const li = bubble('him', 'Jarvis is thinking...', true);
  let after = -1;
  let misses = 0;
  try {
    for (;;) {
      let r;
      try {
        r = await api('/api/turn?id=' + encodeURIComponent(turn) + '&after=' + after + '&wait=25', undefined, 40000);
        misses = 0;
      } catch (e) {
        if (e.status === 401) { store.set('token', null); show('pair'); return; }
        if (++misses > 4) throw e;
        await new Promise((ok) => setTimeout(ok, 1500 * misses));
        continue;
      }
      after = r.version;
      if (r.text) { li.classList.remove('wait'); addText(li, r.text); li.scrollIntoView({block: 'end'}); }
      if (r.done) {
        remember('him', r.text);
        if (r.ask) askButtons(li, r.ask);
        setState('online', 'ok');
        await speak(r.text, turn);
        return;
      }
    }
  } catch (e) {
    li.classList.remove('wait');
    addText(li, "Lost the connection to Jarvis while he was answering. He may still finish on the laptop.");
    setState('offline', 'bad');
  } finally {
    busy = false;
    $('send').disabled = false;
    listenSoon();
  }
}

async function send(text) {
  text = (text || '').trim();
  if (!text || busy) return;
  stopListening();
  $('text').value = '';
  grow();
  bubble('me', text);
  remember('me', text);
  try {
    const r = await api('/api/send', {text});
    await follow(r.turn);
  } catch (e) {
    if (e.status === 401) { store.set('token', null); show('pair'); return; }
    bubble('him', e.status === 409 ? "I'm still answering your last message." : e.message +
      '. Check that the laptop is on with Jarvis open.');
    setState('offline', 'bad');
    listenSoon();
  }
}

function grow() {
  const t = $('text');
  t.style.height = 'auto';
  t.style.height = Math.min(t.scrollHeight, 140) + 'px';
}

// ---- voice -----------------------------------------------------------------------------------------------
// Replies are read in Jarvis's own Settings voice, made on the laptop; the phone's built-in voice is the backup.
// Hands-free works like the desktop's live voice: listen, send, read the answer aloud, then listen again. Phones
// only let a web app listen while it is open on the screen, so it pauses when the app is hidden.
const YES_RE = /^(yes|yeah|yep|yup|sure|ok(ay)?|go ahead|do it|please do)\b/i;
const NO_RE = /^(no|nope|nah|don'?t|do not|cancel)\b/i;
const OFF_RE = /^(stop listening|hands[- ]?free off|turn off hands[- ]?free|goodbye|bye)\b/i;
const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
const player = new Audio();
let talking = null;  // ends the reply being read aloud
let rec = null;
let quietEnds = 0;  // listens in a row that ended quickly without words, to back off
let wakeLock = null;

function speakable(text) {
  return text.replace(/```[\s\S]*?(```|$)/g, ' ').replace(URL_RE, 'the link').replace(/[*_`#>|~]/g, '')
    .replace(/\s+/g, ' ').trim();
}

function setMic() {
  const mic = $('mic');
  mic.classList.toggle('speaking', !!talking);
  mic.classList.toggle('listening', !talking && !!rec);
  mic.textContent = talking ? 'Interrupt' : rec ? 'Stop' : 'Talk';
}

function finishTalking() {
  if (!talking) return;
  const done = talking;
  talking = null;
  player.pause();
  if ('speechSynthesis' in window) speechSynthesis.cancel();
  setMic();
  done();
}

function phoneVoice(words) {
  if (!('speechSynthesis' in window)) { finishTalking(); return; }
  const mine = talking;
  const say = new SpeechSynthesisUtterance(words.slice(0, 3000));
  say.onend = say.onerror = () => { if (talking === mine) finishTalking(); };
  speechSynthesis.cancel();
  speechSynthesis.speak(say);
  setTimeout(() => { if (talking === mine) finishTalking(); }, 8000 + words.length * 90);  // onend can go missing
}

async function speak(text, turn) {
  if (!text || !(handsFree || store.get('speak') === '1')) return;
  const words = speakable(text);
  if (!words) return;
  finishTalking();
  const over = new Promise((ok) => { talking = ok; });
  setMic();
  let src = null;
  try {
    const resp = await request('/api/speech', {turn}, 60000);
    if (!resp.ok) throw new ApiError(resp.status, 'no voice');
    const blob = await resp.blob();
    if (!talking) return;  // interrupted while the voice was being made
    src = URL.createObjectURL(blob);
    player.src = src;
    player.onended = player.onerror = finishTalking;
    await player.play();
  } catch (e) {
    if (talking) phoneVoice(words);
  }
  await over;
  if (src) URL.revokeObjectURL(src);
}

function listenSoon(delay = 300) {
  if (handsFree) setTimeout(listen, delay);
}

function listen() {
  if (handsFree && !busy && !rec && !talking && !document.hidden) startRec();
}

function startRec() {
  const r = new Recognition();
  rec = r;
  r.lang = navigator.language || 'en-US';
  r.interimResults = true;
  r.continuous = false;
  let got = false;
  const began = Date.now();
  r.onresult = (ev) => {
    const said = Array.from(ev.results).map((x) => x[0].transcript).join('');
    $('text').value = said;
    grow();
    if (!got && ev.results[ev.results.length - 1].isFinal) {
      got = true;
      heard(said.trim());
    }
  };
  r.onerror = (ev) => {
    if (['not-allowed', 'service-not-allowed', 'audio-capture'].includes(ev.error)) {
      setHandsFree(false);
      banner("Jarvis can't hear you: allow the microphone for this app in the phone's settings, then try again.");
    }
  };
  r.onend = () => {
    if (rec === r) rec = null;
    setMic();
    if (got) { quietEnds = 0; return; }
    quietEnds = Date.now() - began < 2000 ? quietEnds + 1 : 0;
    listenSoon(Math.min(300 * 2 ** quietEnds, 5000));
  };
  try { r.start(); } catch (e) { rec = null; }
  setMic();
}

function stopListening() {
  if (rec) rec.abort();
}

function heard(said) {
  const clear = () => { $('text').value = ''; grow(); };
  if (!said) { listenSoon(); return; }
  if (handsFree && OFF_RE.test(said)) { clear(); setHandsFree(false); return; }
  if (openAsk && YES_RE.test(said)) { clear(); answerAsk(true); return; }
  if (openAsk && NO_RE.test(said)) { clear(); answerAsk(false); return; }
  send(said);
}

function keepAwake() {
  if (!handsFree || !('wakeLock' in navigator) || wakeLock) return;
  navigator.wakeLock.request('screen').then((lock) => {
    wakeLock = lock;
    lock.addEventListener('release', () => { if (wakeLock === lock) wakeLock = null; });
  }).catch(() => {});
}

function setHandsFree(on) {
  handsFree = on;
  $('handsFree').setAttribute('aria-pressed', String(on));
  $('handsFree').textContent = on ? 'Hands-free on' : 'Hands-free';
  if (on) {
    quietEnds = 0;
    keepAwake();
    listenSoon(0);
  } else {
    stopListening();
    finishTalking();
    if (wakeLock) { wakeLock.release().catch(() => {}); wakeLock = null; }
  }
  setMic();
}

function showVoiceButton() {
  const on = store.get('speak') === '1';
  $('speak').setAttribute('aria-pressed', String(on));
  $('speak').textContent = on ? 'Voice on' : 'Voice off';
}

function setupVoice() {
  showVoiceButton();
  $('speak').onclick = () => {
    store.set('speak', store.get('speak') === '1' ? '0' : '1');
    if (store.get('speak') !== '1' && !handsFree) finishTalking();
    showVoiceButton();
  };
  if (!Recognition) return;
  $('mic').hidden = false;
  $('handsFree').hidden = false;
  $('mic').onclick = () => {
    if (talking) { finishTalking(); return; }  // interrupt Jarvis; hands-free then listens again
    if (rec) { if (handsFree) setHandsFree(false); else rec.stop(); return; }
    if (handsFree) listen(); else startRec();
  };
  $('handsFree').onclick = () => setHandsFree(!handsFree);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { stopListening(); return; }
    keepAwake();
    listenSoon();
  });
}

// ---- install hint ----------------------------------------------------------------------------------------
function setupInstall() {
  const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone;
  if (standalone || store.get('installTip') === 'done') return;
  const tip = $('install');
  if (/iphone|ipad/i.test(navigator.userAgent)) {
    tip.textContent = 'To keep Jarvis as an app: tap the Share button, then "Add to Home Screen".';
    tip.hidden = false;
  }
  addEventListener('beforeinstallprompt', (ev) => {
    ev.preventDefault();
    tip.textContent = 'Keep Jarvis on your home screen. ';
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = 'Install app';
    b.onclick = async () => { ev.prompt(); await ev.userChoice; tip.hidden = true; store.set('installTip', 'done'); };
    tip.append(b);
    tip.hidden = false;
  });
}

// ---- start -----------------------------------------------------------------------------------------------
addEventListener('DOMContentLoaded', () => {
  $('send').onclick = () => send($('text').value);
  $('text').addEventListener('input', grow);
  $('text').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && !ev.shiftKey && !/android|iphone|ipad/i.test(navigator.userAgent)) {
      ev.preventDefault();
      send($('text').value);
    }
  });
  $('linkSave').onclick = () => {
    const found = readLink($('linkInput').value);
    if (!found.host && !base()) {
      $('setupError').textContent = "That isn't a Jarvis phone link.";
      $('setupError').hidden = false;
      return;
    }
    useLink(found);
  };
  $('codeSave').onclick = () => {
    const code = $('codeInput').value.trim();
    if (!CODE_RE.test(code)) { $('pairError').textContent = 'The code has 8 digits.'; $('pairError').hidden = false; return; }
    pair(code);
  };
  setupVoice();
  setupInstall();
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js').catch(() => {});
  const found = readLink(location.hash);
  if (location.hash) window.history.replaceState(null, '', location.pathname + location.search);
  if (found.host || found.code) useLink(found); else start();
});
