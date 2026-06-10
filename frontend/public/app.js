// ComputeAgent browser UI. No build step: nginx serves this file as an ES module.

const API = '/api/v1';
const TOKENS_KEY = 'computeagent_session_tokens';
const API_KEY_KEY = 'computeagent_api_key';
const POLL_MS = 5000;
const RECONNECT_MS = 2000;

const $ = (id) => document.getElementById(id);

// ── Storage ────────────────────────────────────────────────────────────────
// The API has no accounts: each session is reachable only with the token
// returned when it was created, so this browser keeps its own id -> token map.

function readJson(key) {
  try {
    return JSON.parse(localStorage.getItem(key)) || {};
  } catch {
    return {};
  }
}

export const tokens = {
  all: () => readJson(TOKENS_KEY),
  get: (id) => readJson(TOKENS_KEY)[id] || '',
  set(id, token) {
    const all = readJson(TOKENS_KEY);
    if (token) all[id] = token;
    else delete all[id];
    localStorage.setItem(TOKENS_KEY, JSON.stringify(all));
  },
};

// The API key stays in this tab only, unless the user asks to remember it.
export const apiKey = {
  get: () => sessionStorage.getItem(API_KEY_KEY) || localStorage.getItem(API_KEY_KEY) || '',
  save(value, remember) {
    sessionStorage.setItem(API_KEY_KEY, value);
    if (remember) localStorage.setItem(API_KEY_KEY, value);
    else localStorage.removeItem(API_KEY_KEY);
  },
  remembered: () => Boolean(localStorage.getItem(API_KEY_KEY)),
};

// ── Small DOM helpers ──────────────────────────────────────────────────────

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const child of children) if (child != null) node.append(child);
  return node;
}

export function shortId(id) {
  return `${id.slice(0, 8)}…`;
}

export function modelLabel(model) {
  return model.replace(/^claude-/, '').replace(/-\d{8}$/, '');
}

function badge(node, status) {
  node.className = `badge ${status}`;
  node.textContent = status;
}

// ── State ──────────────────────────────────────────────────────────────────

const state = {
  sessions: {},
  activeId: null,
  ws: null,
  reconnectTimer: null,
};

async function api(path, { method = 'GET', id, body } = {}) {
  const headers = {};
  if (id) headers.Authorization = `Bearer ${tokens.get(id)}`;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const res = await fetch(`${API}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return res;
}

async function errorText(res) {
  try {
    const data = await res.json();
    if (typeof data.detail === 'string') return data.detail;
    if (Array.isArray(data.detail)) return data.detail.map((d) => d.msg).join('; ');
  } catch {
    /* fall through to the status line */
  }
  return `${res.status} ${res.statusText}`.trim();
}

// ── Sidebar ────────────────────────────────────────────────────────────────

function setListState(message, { retry = false } = {}) {
  const box = $('sessionsState');
  box.replaceChildren();
  box.hidden = !message;
  if (!message) return;
  box.append(el('p', { text: message }));
  if (retry) {
    const btn = el('button', { type: 'button', class: 'btn', text: 'Try again' });
    btn.addEventListener('click', loadSessions);
    box.append(btn);
  }
}

export function renderSidebar() {
  const list = $('sessionsList');
  const ids = Object.keys(state.sessions).sort(
    (a, b) => new Date(state.sessions[b].created_at) - new Date(state.sessions[a].created_at),
  );
  list.replaceChildren(
    ...ids.map((id) => {
      const s = state.sessions[id];
      const status = el('span');
      badge(status, s.status);
      const btn = el(
        'button',
        { type: 'button', class: 'session-item', 'aria-current': String(id === state.activeId) },
        el('span', { class: 'sid', text: shortId(id) }),
        status,
        el('span', { class: 'smodel', text: modelLabel(s.model) }),
      );
      btn.addEventListener('click', () => selectSession(id));
      return el('li', {}, btn);
    }),
  );
  if (!ids.length && $('sessionsState').hidden) setListState('No sessions yet. Start one to get a desktop.');
  else if (ids.length) setListState('');
}

export async function loadSessions() {
  setListState('Loading sessions…');
  let unreachable = false;
  for (const id of Object.keys(tokens.all())) {
    try {
      const res = await api(`/sessions/${id}`, { id });
      if (res.status === 401 || res.status === 404) tokens.set(id, null);
      else if (res.ok) state.sessions[id] = await res.json();
      else unreachable = true;
    } catch {
      unreachable = true;
    }
  }
  if (unreachable && !Object.keys(state.sessions).length) {
    setListState('Could not reach the API.', { retry: true });
    return;
  }
  setListState('');
  renderSidebar();
}

async function refreshStatuses() {
  for (const id of Object.keys(state.sessions)) {
    try {
      const res = await api(`/sessions/${id}`, { id });
      if (!res.ok) continue;
      state.sessions[id] = await res.json();
      if (id === state.activeId) updateToolbar();
    } catch {
      /* transient; the next poll retries */
    }
  }
  renderSidebar();
}

// ── Session view ───────────────────────────────────────────────────────────

function active() {
  return state.sessions[state.activeId];
}

function updateToolbar() {
  const s = active();
  if (!s) return;
  $('toolbarId').textContent = shortId(s.id);
  $('toolbarModel').textContent = modelLabel(s.model);
  badge($('toolbarStatus'), s.status);
  $('sendBtn').disabled = s.status !== 'idle';
  $('terminateBtn').disabled = s.status === 'terminated';
}

function setStatus(status) {
  const s = active();
  if (!s) return;
  s.status = status;
  updateToolbar();
  renderSidebar();
}

export function selectSession(id) {
  state.activeId = id;
  $('welcome').hidden = true;
  $('sessionView').hidden = false;
  $('feed').replaceChildren();
  $('feedEmpty').hidden = false;
  updateToolbar();
  renderSidebar();
  setupVnc();
  switchTab('feed');
  connectStream(id);
}

function setupVnc() {
  const s = active();
  const panel = $('vncPanel');
  const linkRow = $('vncLinkRow');
  if (s?.vnc_info?.novnc_url && s.status !== 'terminated') {
    const url = new URL(s.vnc_info.novnc_url, location.origin).toString();
    panel.replaceChildren(el('iframe', { src: url, title: 'Live session desktop', allowfullscreen: '' }));
    $('vncOpenLink').href = url;
    linkRow.hidden = false;
  } else {
    panel.replaceChildren(el('p', { class: 'empty', text: `The desktop is not available (status: ${s?.status ?? 'none'}).` }));
    linkRow.hidden = true;
  }
}

// ── Feed ───────────────────────────────────────────────────────────────────

export function appendEntry(kind, label, ...body) {
  const feed = $('feed');
  feed.append(el('li', { class: `entry ${kind}` }, el('span', { class: 'label', text: label }), ...body));
  $('feedEmpty').hidden = true;
  feed.scrollTop = feed.scrollHeight;
}

function screenshot(base64) {
  const src = `data:image/png;base64,${base64}`;
  const btn = el(
    'button',
    { type: 'button', class: 'shot', 'aria-label': 'Enlarge screenshot' },
    el('img', { src, alt: 'Screenshot of the session desktop' }),
  );
  btn.addEventListener('click', () => openLightbox(src));
  return btn;
}

export function handleEvent(ev) {
  if (!ev || typeof ev.type !== 'string') return;
  switch (ev.type) {
    case 'status': {
      const previous = active()?.status;
      if (ev.status === 'connecting' || ev.status === 'connected') {
        appendEntry('status', 'Stream', ev.message || ev.status);
        break;
      }
      if (ev.status) setStatus(ev.status);
      if (ev.status === 'idle' && previous === 'running') finishTask();
      break;
    }
    case 'output': {
      const d = ev.data || {};
      if (d.type === 'text' && d.text) appendEntry('output', 'Claude', el('p', { text: d.text }));
      else if (d.type === 'thinking' && d.thinking) appendEntry('output', 'Thinking', el('p', { text: d.thinking.slice(0, 400) }));
      else if (d.type === 'tool_use') {
        appendEntry('output', `Tool call · ${d.name}`, el('pre', { text: JSON.stringify(d.input, null, 2) }));
      }
      break;
    }
    case 'tool_result': {
      const parts = [];
      if (ev.error) parts.push(el('p', { class: 'error', text: String(ev.error) }));
      if (ev.output) parts.push(el('pre', { text: String(ev.output).slice(0, 800) }));
      if (ev.base64_image) parts.push(screenshot(ev.base64_image));
      if (parts.length) appendEntry('tool', `Tool result · ${String(ev.tool_id || '').slice(0, 12)}`, ...parts);
      break;
    }
    case 'done':
      setStatus('idle');
      finishTask();
      break;
    case 'error':
      appendEntry('error', 'Error', el('p', { text: ev.message || 'Unknown error' }));
      break;
    default:
      break;
  }
}

function finishTask() {
  const last = $('feed').lastElementChild;
  if (!last?.classList.contains('complete')) appendEntry('complete', 'Ready', 'Claude is idle and ready for the next task.');
  $('taskHint').textContent = 'Ready for the next task.';
  $('taskInput').focus();
  loadHistory(state.activeId);
}

function connectStream(id) {
  clearTimeout(state.reconnectTimer);
  if (state.ws) {
    state.ws.onclose = null;
    state.ws.close();
  }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = `${proto}://${location.host}${API}/sessions/${id}/stream?token=${encodeURIComponent(tokens.get(id))}`;
  const ws = new WebSocket(url);
  state.ws = ws;
  ws.onmessage = ({ data }) => {
    try {
      handleEvent(JSON.parse(data));
    } catch {
      /* ignore malformed frames */
    }
  };
  ws.onclose = () => {
    const s = state.sessions[id];
    if (id === state.activeId && s && !['terminated', 'error'].includes(s.status)) {
      appendEntry('status', 'Stream', 'Connection lost, reconnecting…');
      state.reconnectTimer = setTimeout(() => connectStream(id), RECONNECT_MS);
    }
  };
}

// ── Tasks ──────────────────────────────────────────────────────────────────

export async function sendTask() {
  const input = $('taskInput');
  const text = input.value.trim();
  const s = active();
  if (!text || !s || s.status !== 'idle') return;

  input.value = '';
  appendEntry('you', 'You', el('p', { text }));
  $('sendBtn').disabled = true;
  try {
    const res = await api(`/sessions/${s.id}/message`, { method: 'POST', id: s.id, body: { text } });
    if (res.ok) {
      setStatus('running');
      $('taskHint').textContent = 'Claude is working. Screenshots and tool calls appear above.';
    } else {
      appendEntry('error', 'Not sent', el('p', { text: await errorText(res) }));
      updateToolbar();
    }
  } catch (e) {
    appendEntry('error', 'Network', el('p', { text: e.message }));
    updateToolbar();
  }
}

// ── History ────────────────────────────────────────────────────────────────

export async function loadHistory(id) {
  if (!id) return;
  const panel = $('historyPanel');
  panel.replaceChildren(el('p', { class: 'empty', text: 'Loading history…' }));
  try {
    const res = await api(`/sessions/${id}/history`, { id });
    if (!res.ok) throw new Error(await errorText(res));
    const { messages } = await res.json();
    const bubbles = messages
      .map((m) => {
        const text = m.content
          .map((c) => c.text || (c.type === 'tool_use' ? `[${c.name}]` : ''))
          .filter(Boolean)
          .join('\n');
        if (!text) return null;
        const when = new Date(m.created_at);
        return el(
          'div',
          { class: `bubble ${m.role}` },
          text,
          el('time', { datetime: m.created_at, text: when.toLocaleTimeString() }),
        );
      })
      .filter(Boolean);
    panel.replaceChildren(...(bubbles.length ? bubbles : [el('p', { class: 'empty', text: 'No messages yet.' })]));
  } catch (e) {
    panel.replaceChildren(el('p', { class: 'empty error', text: `Could not load history: ${e.message}` }));
  }
}

// ── Terminate ──────────────────────────────────────────────────────────────

async function terminate() {
  const s = active();
  if (!s || !window.confirm('Terminate this session and destroy its container?')) return;
  const res = await api(`/sessions/${s.id}`, { method: 'DELETE', id: s.id });
  if (!res.ok && res.status !== 404) {
    appendEntry('error', 'Terminate failed', el('p', { text: await errorText(res) }));
    return;
  }
  setStatus('terminated');
  setupVnc();
  if (state.ws) {
    state.ws.onclose = null;
    state.ws.close();
    state.ws = null;
  }
}

// ── New session ────────────────────────────────────────────────────────────

function showDialog(dialog) {
  if (typeof dialog.showModal === 'function') dialog.showModal();
  else dialog.setAttribute('open', '');
}

function closeDialog(dialog) {
  if (typeof dialog.close === 'function') dialog.close();
  else dialog.removeAttribute('open');
}

function openNewSession() {
  if (!apiKey.get()) {
    const status = $('apiKeyStatus');
    status.textContent = 'Save your API key first; sessions need it to call Claude.';
    status.className = 'hint error';
    $('apiKeyInput').focus();
    return;
  }
  $('createError').hidden = true;
  showDialog($('newSessionDialog'));
  $('modelSelect').focus();
}

export async function createSession(event) {
  event?.preventDefault();
  const btn = $('createBtn');
  const errorBox = $('createError');
  errorBox.hidden = true;
  btn.disabled = true;
  btn.textContent = 'Starting container…';
  try {
    const res = await api('/sessions', {
      method: 'POST',
      body: {
        api_key: apiKey.get(),
        model: $('modelSelect').value,
        tool_version: $('toolVersion').value,
        provider: 'anthropic',
      },
    });
    if (!res.ok) {
      errorBox.textContent = `Could not start the session: ${await errorText(res)}`;
      errorBox.hidden = false;
      return;
    }
    const { access_token: token, ...session } = await res.json();
    tokens.set(session.id, token);
    state.sessions[session.id] = session;
    closeDialog($('newSessionDialog'));
    selectSession(session.id);
  } catch (e) {
    errorBox.textContent = `Could not reach the API: ${e.message}`;
    errorBox.hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Start session';
  }
}

// ── Tabs (WAI-ARIA tabs pattern) ───────────────────────────────────────────

const TABS = ['feed', 'vnc', 'history'];

export function switchTab(name) {
  for (const t of TABS) {
    const tab = $(`tab-${t}`);
    const selected = t === name;
    tab.setAttribute('aria-selected', String(selected));
    tab.tabIndex = selected ? 0 : -1;
    $(`panel-${t}`).hidden = !selected;
  }
  if (name === 'history') loadHistory(state.activeId);
}

function onTabKey(e) {
  const i = TABS.findIndex((t) => `tab-${t}` === e.target.id);
  let next = null;
  if (e.key === 'ArrowRight') next = TABS[(i + 1) % TABS.length];
  else if (e.key === 'ArrowLeft') next = TABS[(i - 1 + TABS.length) % TABS.length];
  else if (e.key === 'Home') next = TABS[0];
  else if (e.key === 'End') next = TABS[TABS.length - 1];
  if (!next) return;
  e.preventDefault();
  switchTab(next);
  $(`tab-${next}`).focus();
}

// ── Lightbox ───────────────────────────────────────────────────────────────

function openLightbox(src) {
  $('lightboxImg').src = src;
  showDialog($('lightbox'));
}

// ── Wiring ─────────────────────────────────────────────────────────────────

export async function start() {
  const key = apiKey.get();
  if (key) {
    $('apiKeyInput').value = key;
    $('rememberKey').checked = apiKey.remembered();
  }

  $('apiKeyForm').addEventListener('submit', (e) => {
    e.preventDefault();
    const value = $('apiKeyInput').value.trim();
    const status = $('apiKeyStatus');
    if (!/^[\x21-\x7E]+$/.test(value)) {
      status.textContent = 'That doesn’t look like a key. Paste the whole key without spaces.';
      status.className = 'hint error';
      return;
    }
    apiKey.save(value, $('rememberKey').checked);
    status.textContent = $('rememberKey').checked ? 'Saved on this device.' : 'Saved for this tab.';
    status.className = 'hint ok';
  });

  $('newSessionBtn').addEventListener('click', openNewSession);
  $('newSessionForm').addEventListener('submit', createSession);
  $('cancelCreate').addEventListener('click', () => closeDialog($('newSessionDialog')));
  $('modelSelect').addEventListener('change', (e) => {
    const tool = e.target.selectedOptions[0]?.dataset.tool;
    if (tool) $('toolVersion').value = tool;
  });

  $('taskForm').addEventListener('submit', (e) => {
    e.preventDefault();
    sendTask();
  });
  $('taskInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendTask();
    }
  });
  $('terminateBtn').addEventListener('click', terminate);

  for (const t of TABS) {
    $(`tab-${t}`).addEventListener('click', () => switchTab(t));
    $(`tab-${t}`).addEventListener('keydown', onTabKey);
  }
  $('closeLightbox').addEventListener('click', () => closeDialog($('lightbox')));

  await loadSessions();
  return setInterval(refreshStatuses, POLL_MS);
}

export { state as _state };

if (!globalThis.__COMPUTEAGENT_TEST__) start();
