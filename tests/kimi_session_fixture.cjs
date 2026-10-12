const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2], 'utf8')
  .replace('__CAPTURE_NONCE__', 'owned-pairing-nonce').replace('__CAPTURE_PORT__', '12345')
  .replace('__CAPTURE_EXPIRES_MS__', String(Date.now() + 60000));

function fixture(origin, expired = false) {
  let reads = 0;
  const requests = [];
  const elements = [];
  const window = {};
  window.top = window;
  const context = {
    Date: { now: () => Date.now() + (expired ? 120000 : 0) }, setTimeout() {},
    window, location: { origin }, navigator: { userAgent: 'owned-agent' },
    localStorage: { getItem(key) { assert.ok(['access_token', 'refresh_token'].includes(key)); reads++; return key === 'access_token' ? 'owned-token' : 'owned-refresh'; } },
    GM: { async xmlHttpRequest(request) { requests.push(request); return { status: 200 }; } },
    document: {
      documentElement: { append() {} },
      createElement(tag) {
        const element = { tag, style: {}, attachShadow() { return { append() {} }; },
          addEventListener(event, listener) { assert.equal(event, 'click'); this.click = listener; } };
        elements.push(element);
        return element;
      },
    },
  };
  vm.runInNewContext(source, context, { timeout: 1000 });
  return { elements, requests, reads: () => reads };
}

(async () => {
  const other = fixture('https://www.kimi.ai.evil.invalid');
  assert.equal(other.elements.length, 0);
  assert.equal(other.reads(), 0);
  const expired = fixture('https://www.kimi.ai', true);
  const expiredButton = expired.elements.find(element => element.tag === 'button');
  assert.ok(expiredButton.disabled);
  assert.match(expiredButton.textContent, /expired/);
  await expiredButton.click({ isTrusted: true });
  assert.equal(expired.reads(), 0);
  assert.equal(expired.requests.length, 0);
  const own = fixture('https://www.kimi.ai');
  const button = own.elements.find(element => element.tag === 'button');
  assert.ok(button);
  assert.equal(own.reads(), 0);
  await button.click({ isTrusted: false });
  assert.equal(own.reads(), 0);
  assert.equal(own.requests.length, 0);
  await button.click({ isTrusted: true });
  assert.equal(own.reads(), 2);
  assert.equal(own.requests.length, 1);
  assert.equal(own.requests[0].url, 'http://127.0.0.1:12345/capture');
  assert.equal(own.requests[0].headers.Authorization, 'Bearer owned-pairing-nonce');
  assert.deepEqual(JSON.parse(own.requests[0].data), {
    origin: 'https://www.kimi.ai', token: 'owned-token', refresh_token: 'owned-refresh', user_agent: 'owned-agent',
  });
  assert.equal(button.textContent, 'Session saved locally · close this tab');
  await button.click({ isTrusted: true });
  assert.equal(own.requests.length, 1);
  assert.equal(own.reads(), 2);
})().catch(error => { console.error(error); process.exitCode = 1; });
