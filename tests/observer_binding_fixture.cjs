// Actual observer in an isolated DOM/fetch fixture. No sockets or browser account.
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(require('node:path').join(__dirname, '../browser/opencode-observer.user.js'), 'utf8');
class CustomEvent extends Event { constructor(type, options) { super(type); this.detail = options.detail; } }

class FakeWebSocket extends EventTarget {
  send() {}
}
FakeWebSocket.prototype.send = function() {};

async function check(host, requests, { path = '/c/expected', prompt = 'owned prompt' } = {}) {
  const document = new EventTarget(), receipts = [];
  const location = { hostname: host, origin: 'https://' + host, pathname: path || '/' };
  let calls = 0;
  const chatgpt = host === 'chatgpt.com' || host === 'chat.openai.com';
  const window = {
    fetch: async () => {
      calls++;
      // ChatGPT observer ignores non-SSE conversation helpers (prepare/finalize).
      const headers = chatgpt ? { 'Content-Type': 'text/event-stream' } : undefined;
      return new Response(
        chatgpt ? 'data: {"v":"ok"}\n\ndata: [DONE]\n\n' : 'answer',
        { status: 200, headers },
      );
    },
    WebSocket: FakeWebSocket,
  };
  document.addEventListener('opencode-local-response-v1', e => receipts.push(JSON.parse(e.detail)));
  vm.runInNewContext(source, {
    window, document, location, CustomEvent, Request, Response, URL,
    Uint8Array, DataView, TextDecoder, TextEncoder, btoa, JSON, WebSocket: FakeWebSocket,
  });
  document.dispatchEvent(new CustomEvent('opencode-local-job-v1', { detail: JSON.stringify({ nonce: 'owned', prompt, path }) }));
  for (const [endpoint, body] of requests) {
    await window.fetch(location.origin + endpoint, { method: 'POST', body: JSON.stringify(body) });
    await new Promise(resolve => setTimeout(resolve, chatgpt ? 40 : 15));
  }
  const captured = receipts.filter(r => r.body);
  assert.equal(calls, requests.length, 'observer must not duplicate or suppress site requests');
  assert.equal(captured.length, 1);
  return captured[0];
}

async function checkGrokWs() {
  const document = new EventTarget(), receipts = [];
  const location = { hostname: 'grok.com', origin: 'https://grok.com', pathname: '/' };
  const window = { fetch: async () => new Response('nope', { status: 200 }), WebSocket: FakeWebSocket };
  document.addEventListener('opencode-local-response-v1', e => receipts.push(JSON.parse(e.detail)));
  vm.runInNewContext(source, {
    window, document, location, CustomEvent, Request, Response, URL,
    Uint8Array, DataView, TextDecoder, TextEncoder, btoa, JSON, WebSocket: FakeWebSocket,
  });
  document.dispatchEvent(new CustomEvent('opencode-local-job-v1', {
    detail: JSON.stringify({ nonce: 'ws', prompt: 'owned prompt', path: null }),
  }));
  const socket = new FakeWebSocket();
  // Patched prototype.send from the observer
  FakeWebSocket.prototype.send.call(socket, JSON.stringify({
    session_id: 'sess',
    event: {
      type: 'response.create',
      item: { type: 'message', role: 'user', x_grok: { input_chunks: [{ text: { text: 'owned prompt' } }] } },
    },
  }));
  socket.dispatchEvent(new MessageEvent('message', {
    data: JSON.stringify({ event: { type: 'conversation.attached', conversation: { id: 'ws-chat' } } }),
  }));
  socket.dispatchEvent(new MessageEvent('message', {
    data: JSON.stringify({
      event: {
        type: 'response.chunk',
        chunk: { text: { text: 'Hello ', channel: 'CHANNEL_ASSISTANT_RESPONSE' } },
      },
    }),
  }));
  socket.dispatchEvent(new MessageEvent('message', {
    data: JSON.stringify({
      event: {
        type: 'response.chunk',
        chunk: { text: { text: 'world', channel: 'CHANNEL_ASSISTANT_RESPONSE' } },
      },
    }),
  }));
  socket.dispatchEvent(new MessageEvent('message', {
    data: JSON.stringify({ event: { type: 'response.done' } }),
  }));
  await new Promise(resolve => setTimeout(resolve, 20));
  const captured = receipts.filter(r => r.body);
  assert.equal(captured.length, 1);
  assert.equal(captured[0].request_turn.chat_id, 'ws-chat');
  assert.equal(captured[0].request_turn.endpoint, '/rest/app-chat/conversations/ws-chat/responses');
  const body = Buffer.from(captured[0].body, 'base64').toString('utf8');
  assert.match(body, /Hello world/);
  assert.match(body, /"partial":false/);
}

(async () => {
  const resumed = await check('grok.com', [
    ['/rest/app-chat/conversations/other/responses', { message: 'owned prompt' }],
    ['/rest/app-chat/conversations/expected/responses', { message: 'different', history: [{ message: 'owned prompt' }] }],
    ['/rest/app-chat/conversations/new', { message: 'owned prompt' }],
    ['/rest/app-chat/conversations/expected/responses', { message: 'owned prompt' }],
  ]);
  assert.equal(resumed.request_turn.chat_id, 'expected');
  assert.equal(resumed.request_turn.prompt, 'owned prompt');
  assert.equal(resumed.request_turn.endpoint, '/rest/app-chat/conversations/expected/responses');

  const fresh = await check('grok.com', [
    ['/rest/app-chat/conversations/new', { message: 'other tab noise' }],
    ['/rest/app-chat/conversations/fresh-id/responses', { query: 'rewritten' }],
  ], { path: null });
  assert.equal(fresh.request_turn.chat_id, null);
  assert.equal(fresh.request_turn.endpoint, '/rest/app-chat/conversations/new');

  const preallocated = await check('grok.com', [
    ['/rest/app-chat/conversations/prealloc/responses', { opaque: true }],
  ], { path: null });
  assert.equal(preallocated.request_turn.chat_id, 'prealloc');
  assert.equal(preallocated.request_turn.endpoint, '/rest/app-chat/conversations/prealloc/responses');

  await check('chat.z.ai', [
    ['/api/v2/chat/completions', { chat_id: 'other', messages: [{ role: 'user', content: 'owned prompt' }] }],
    ['/api/v2/chat/completions', { chat_id: 'expected', messages: [{ role: 'user', content: 'owned prompt' }, { role: 'user', content: 'different' }] }],
    ['/api/v2/chat/completions', { chat_id: 'expected', messages: [{ role: 'user', content: 'owned prompt' }] }],
  ]);

  // Prepare helpers must not steal exclusive capture from the real SSE turn.
  const chatgpt = await check('chatgpt.com', [
    ['/backend-api/f/conversation/prepare', { conversation_id: 'expected', messages: [{ content: { parts: ['owned prompt'] } }] }],
    ['/backend-api/f/conversation', { conversation_id: 'other', messages: [{ content: { parts: ['owned prompt'] } }], action: 'next' }],
    ['/backend-api/f/conversation', { conversation_id: 'expected', messages: [{ content: { parts: ['owned prompt'] } }], action: 'next' }],
  ]);
  assert.equal(chatgpt.path, '/c/expected');

  await checkGrokWs();
  console.log('PASS: Grok REST + mgw WebSocket capture + GLM exact binding + ChatGPT');
})().catch(error => { console.error(error); process.exitCode = 1; });
