import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const html = readFileSync(resolve(import.meta.dirname, '../public/index.html'), 'utf8');
const body = html.match(/<body>([\s\S]*)<\/body>/)[1].replace(/<script[\s\S]*?<\/script>/g, '');

const SID = '3f65d497-de20-4e18-bc28-31a9a2f02618';
const session = (over = {}) => ({
  id: SID,
  status: 'idle',
  model: 'claude-sonnet-4-5-20250929',
  provider: 'anthropic',
  tool_version: 'computer_use_20250124',
  vnc_info: { vnc_host: '127.0.0.1', vnc_port: 5901, novnc_url: `/novnc/${SID}/vnc.html?autoconnect=true` },
  created_at: '2026-11-10T09:00:00',
  updated_at: '2026-11-10T09:00:00',
  ...over,
});

const json = (status, data) =>
  new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });

class FakeWebSocket {
  static instances = [];
  constructor(url) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }
  close() {}
  emit(event) {
    this.onmessage({ data: JSON.stringify(event) });
  }
}

let routes;
let calls;
let app;

async function boot() {
  app = await import('../public/app.js');
  await app.start();
  return app;
}

const $ = (id) => document.getElementById(id);
const flush = () => new Promise((r) => setTimeout(r, 0));

beforeEach(() => {
  vi.resetModules();
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
  document.body.innerHTML = body;
  localStorage.clear();
  sessionStorage.clear();
  globalThis.__COMPUTEAGENT_TEST__ = true;
  FakeWebSocket.instances = [];
  vi.stubGlobal('WebSocket', FakeWebSocket);
  routes = {};
  calls = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url, opts = {}) => {
      const method = opts.method || 'GET';
      calls.push({ url, method, headers: opts.headers || {}, body: opts.body && JSON.parse(opts.body) });
      const handler = routes[`${method} ${url}`];
      if (!handler) return json(404, { detail: 'Session not found' });
      return handler(opts);
    }),
  );
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('session list', () => {
  it('shows an empty state when this browser has no sessions', async () => {
    await boot();
    expect($('sessionsState').textContent).toContain('No sessions yet');
    expect(fetch).not.toHaveBeenCalled();
  });

  it('loads only the sessions it holds tokens for, with their tokens', async () => {
    localStorage.setItem('computeagent_session_tokens', JSON.stringify({ [SID]: 'tok-1', gone: 'tok-2' }));
    routes[`GET /api/v1/sessions/${SID}`] = () => json(200, session());
    await boot();

    expect(calls.map((c) => c.headers.Authorization)).toEqual(['Bearer tok-1', 'Bearer tok-2']);
    expect($('sessionsList').textContent).toContain('3f65d497');
    expect($('sessionsList').textContent).toContain('sonnet-4-5');
    // A token the API no longer recognises is forgotten.
    expect(JSON.parse(localStorage.getItem('computeagent_session_tokens'))).toEqual({ [SID]: 'tok-1' });
  });

  it('offers a retry when the API is unreachable', async () => {
    localStorage.setItem('computeagent_session_tokens', JSON.stringify({ [SID]: 'tok-1' }));
    fetch.mockRejectedValue(new TypeError('Failed to fetch'));
    await boot();
    expect($('sessionsState').textContent).toContain('Could not reach the API');
    expect($('sessionsState').querySelector('button').textContent).toBe('Try again');
  });
});

describe('api key', () => {
  it('keeps the key for this tab only unless asked to remember it', async () => {
    await boot();
    $('apiKeyInput').value = 'sk-ant-test-not-real';
    $('apiKeyForm').dispatchEvent(new Event('submit', { cancelable: true }));
    expect(sessionStorage.getItem('computeagent_api_key')).toBe('sk-ant-test-not-real');
    expect(localStorage.getItem('computeagent_api_key')).toBeNull();

    $('rememberKey').checked = true;
    $('apiKeyForm').dispatchEvent(new Event('submit', { cancelable: true }));
    expect(localStorage.getItem('computeagent_api_key')).toBe('sk-ant-test-not-real');
  });

  it('asks for a key before opening the new-session dialog', async () => {
    await boot();
    $('newSessionBtn').click();
    expect($('newSessionDialog').open).toBe(false);
    expect($('apiKeyStatus').textContent).toContain('Save your API key first');
  });
});

describe('creating a session', () => {
  beforeEach(() => sessionStorage.setItem('computeagent_api_key', 'sk-ant-test-not-real'));

  it('stores the returned token and streams with it', async () => {
    routes['POST /api/v1/sessions'] = () => json(201, { ...session(), access_token: 'fresh-token' });
    await boot();
    $('newSessionBtn').click();
    await app.createSession();

    const post = calls.find((c) => c.method === 'POST');
    expect(post.body).toMatchObject({ api_key: 'sk-ant-test-not-real', provider: 'anthropic' });
    expect(app.tokens.get(SID)).toBe('fresh-token');
    expect(app._state.sessions[SID].access_token).toBeUndefined();
    expect($('sessionView').hidden).toBe(false);
    expect(FakeWebSocket.instances[0].url).toMatch(/\/api\/v1\/sessions\/3f65d497-.*\/stream\?token=fresh-token$/);
  });

  it('shows the API error inside the dialog', async () => {
    routes['POST /api/v1/sessions'] = () => json(429, { detail: '20 sessions are active (limit 20)' });
    await boot();
    await app.createSession();
    expect($('createError').hidden).toBe(false);
    expect($('createError').textContent).toContain('limit 20');
    expect($('createBtn').disabled).toBe(false);
  });

  it('picks the tool version that matches the chosen model', async () => {
    await boot();
    $('modelSelect').value = 'claude-opus-4-5-20251101';
    $('modelSelect').dispatchEvent(new Event('change'));
    expect($('toolVersion').value).toBe('computer_use_20251124');
  });
});

describe('working in a session', () => {
  beforeEach(() => {
    localStorage.setItem('computeagent_session_tokens', JSON.stringify({ [SID]: 'tok-1' }));
    routes[`GET /api/v1/sessions/${SID}`] = () => json(200, session());
    routes[`GET /api/v1/sessions/${SID}/history`] = () =>
      json(200, {
        session_id: SID,
        messages: [
          { role: 'user', content: [{ type: 'text', text: 'weather in Dubai' }], created_at: '2026-11-10T09:01:00' },
          { role: 'assistant', content: [{ type: 'text', text: 'It is 31C' }], created_at: '2026-11-10T09:02:00' },
        ],
      });
  });

  async function openSession() {
    await boot();
    $('sessionsList').querySelector('button').click();
    return FakeWebSocket.instances.at(-1);
  }

  it('sends a task with the session token and locks Send while running', async () => {
    routes[`POST /api/v1/sessions/${SID}/message`] = () => json(202, {});
    await openSession();
    $('taskInput').value = 'Open the calculator';
    await app.sendTask();

    const post = calls.find((c) => c.method === 'POST');
    expect(post.headers.Authorization).toBe('Bearer tok-1');
    expect(post.body).toEqual({ text: 'Open the calculator' });
    expect($('toolbarStatus').textContent).toBe('running');
    expect($('sendBtn').disabled).toBe(true);
  });

  it('reports a refused task in the feed', async () => {
    routes[`POST /api/v1/sessions/${SID}/message`] = () => json(400, { detail: 'Session not idle (status: running)' });
    await openSession();
    $('taskInput').value = 'again';
    await app.sendTask();
    expect($('feed').textContent).toContain('Session not idle');
    expect($('sendBtn').disabled).toBe(false);
  });

  it('renders streamed text, tool calls and screenshots as inert content', async () => {
    const ws = await openSession();
    ws.emit({ type: 'output', data: { type: 'text', text: '<img src=x onerror="alert(1)">' } });
    ws.emit({ type: 'output', data: { type: 'tool_use', name: '<b>computer</b>', input: { action: 'screenshot' } } });
    ws.emit({ type: 'tool_result', tool_id: 'toolu_01', base64_image: 'iVBORw0KGgo=' });

    const feed = $('feed');
    expect(feed.querySelectorAll('img[onerror]')).toHaveLength(0);
    expect(feed.querySelector('b')).toBeNull();
    expect(feed.textContent).toContain('<img src=x onerror="alert(1)">');
    expect(feed.textContent).toContain('Tool call · <b>computer</b>');
    const shot = feed.querySelector('.shot img');
    expect(shot.getAttribute('src')).toBe('data:image/png;base64,iVBORw0KGgo=');
    expect(shot.alt).toBeTruthy();
  });

  it('returns to idle on done and reloads the history', async () => {
    const ws = await openSession();
    app._state.sessions[SID].status = 'running';
    ws.emit({ type: 'done', messages: [], status: 'idle' });
    await flush();

    expect($('toolbarStatus').textContent).toBe('idle');
    expect($('feed').textContent).toContain('ready for the next task');
    expect($('historyPanel').textContent).toContain('It is 31C');
  });

  it('moves between tabs with the arrow keys', async () => {
    await openSession();
    $('tab-feed').dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
    expect($('tab-vnc').getAttribute('aria-selected')).toBe('true');
    expect($('panel-vnc').hidden).toBe(false);
    expect($('panel-feed').hidden).toBe(true);
    expect($('vncPanel').querySelector('iframe').getAttribute('src')).toContain(`/novnc/${SID}/vnc.html`);
  });

  it('shows a history error instead of a blank panel', async () => {
    routes[`GET /api/v1/sessions/${SID}/history`] = () => json(500, { detail: 'Internal server error' });
    await openSession();
    await app.loadHistory(SID);
    expect($('historyPanel').textContent).toContain('Could not load history: Internal server error');
  });
});
