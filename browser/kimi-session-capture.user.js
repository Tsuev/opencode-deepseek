// ==UserScript==
// @name         Kimi manual local session transfer
// @namespace    opencode-local-bridge
// @version      0.2.0
// @description  One explicit click to transfer the existing Kimi login to the local bridge.
// @match        https://www.kimi.ai/*
// @match        https://www.kimi.com/*
// @inject-into  content
// @run-at       document-end
// @noframes
// @grant        GM.xmlHttpRequest
// ==/UserScript==

(() => {
  'use strict';
  const nonce = '__CAPTURE_NONCE__';
  const port = '__CAPTURE_PORT__';
  const expiresAt = Number('__CAPTURE_EXPIRES_MS__');
  if (window.top !== window || !['https://www.kimi.ai', 'https://www.kimi.com'].includes(location.origin)
      || nonce.startsWith('__') || !/^\d+$/.test(port) || !Number.isFinite(expiresAt)) return;
  const host = document.createElement('div');
  host.style.cssText = 'position:fixed;right:16px;bottom:72px;z-index:2147483647';
  const shadow = host.attachShadow({ mode: 'closed' });
  const button = document.createElement('button');
  button.textContent = 'Save Kimi session locally';
  button.title = 'Save Kimi access and refresh tokens on this Mac for direct HTTP access and session renewal. No prompt is sent.';
  button.style.cssText = 'font:14px system-ui;padding:12px;border:1px solid #777;border-radius:8px;background:white;color:#111';
  shadow.append(button);
  document.documentElement.append(host);
  let submitted = false;
  const expire = () => {
    if (submitted) return;
    button.disabled = true;
    button.textContent = 'Session transfer expired · restart the local capture command';
  };
  if (Date.now() >= expiresAt) expire();
  else setTimeout(expire, expiresAt - Date.now());
  button.addEventListener('click', async event => {
    if (!event.isTrusted || submitted) return;
    if (Date.now() >= expiresAt) { expire(); return; }
    submitted = true;
    button.disabled = true;
    try {
      const token = localStorage.getItem('access_token');
      const refresh_token = localStorage.getItem('refresh_token');
      if (!token || !refresh_token) {
        button.textContent = 'Missing session tokens · sign in to Kimi first';
        return;
      }
      const result = await GM.xmlHttpRequest({
        url: `http://127.0.0.1:${port}/capture`, method: 'POST', timeout: 10000,
        headers: { Authorization: `Bearer ${nonce}`, 'Content-Type': 'application/json' },
        data: JSON.stringify({ origin: location.origin, token, refresh_token, user_agent: navigator.userAgent }),
      });
      if (result.status !== 200) {
        button.textContent = `Local session receiver rejected transfer · HTTP ${Number(result.status) || 0}`;
        return;
      }
      button.textContent = 'Session saved locally · close this tab';
    } catch (_) {
      button.textContent = 'Session not saved · inspect the manual capture command';
    }
  });
})();
