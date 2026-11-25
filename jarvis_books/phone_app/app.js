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

function base() {
  if (HOST_RE.test(location.host)) return location.origin;  // the app is being served by Jarvis itself
  const host = store.get('server');
  return host && HOST_RE.test(host) ? 'https://' + host : null;
}

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(path, body, timeoutMs = 40000) {
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
  let data = {};
  try { data = await resp.json(); } catch (e) { /* not JSON: tunnel error page */ }
  if (!resp.ok) throw new ApiError(resp.status, data.error || ('error ' + resp.status));
  return data;
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

function askButtons(li, ask) {
  const row = document.createElement('div');
  row.className = 'ask';
  for (const [label, yes] of [['Yes', true], ['No', false]]) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    if (!yes) b.className = 'no';
    b.onclick = async () => {
      row.remove();
      bubble('me', label);
      remember('me', label);
      try {
        const r = await api('/api/answer', {request: ask.id, yes});
        await follow(r.turn);
      } catch (e) {
        bubble('him', e.status === 409 ? 'That question is already closed.' : e.message);
      }
    };
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
        speak(r.text);
        setState('online', 'ok');
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
  }
}

async function send(text) {
  text = (text || '').trim();
  if (!text || busy) return;
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
  }
}

function grow() {
  const t = $('text');
  t.style.height = 'auto';
  t.style.height = Math.min(t.scrollHeight, 140) + 'px';
}

// ---- voice -----------------------------------------------------------------------------------------------
function speakable(text) {
  return text.replace(URL_RE, 'link').replace(/[*_`#>|]/g, '').replace(/\s+/g, ' ').trim();
}

function speak(text) {
  if (store.get('speak') !== '1' || !('speechSynthesis' in window) || !text) return;
  speechSynthesis.cancel();
  speechSynthesis.speak(new SpeechSynthesisUtterance(speakable(text).slice(0, 3000)));
}

function setupVoice() {
  const on = store.get('speak') === '1';
  $('speak').setAttribute('aria-pressed', String(on));
  $('speak').textContent = on ? 'Voice on' : 'Voice off';
  $('speak').onclick = () => {
    store.set('speak', store.get('speak') === '1' ? '0' : '1');
    if (store.get('speak') !== '1' && 'speechSynthesis' in window) speechSynthesis.cancel();
    setupVoice();
  };
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) return;
  const mic = $('mic');
  mic.hidden = false;
  let rec = null;
  mic.onclick = () => {
    if (rec) { rec.stop(); return; }
    rec = new Recognition();
    rec.lang = navigator.language || 'en-US';
    rec.interimResults = true;
    rec.onresult = (ev) => {
      const said = Array.from(ev.results).map((r) => r[0].transcript).join('');
      $('text').value = said;
      grow();
      if (ev.results[ev.results.length - 1].isFinal) send(said);
    };
    rec.onend = () => { rec = null; mic.classList.remove('listening'); mic.textContent = 'Talk'; };
    rec.onerror = rec.onend;
    mic.classList.add('listening');
    mic.textContent = 'Stop';
    rec.start();
  };
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
