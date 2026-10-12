// ==UserScript==
// @name         DeepSeek session transfer
// @namespace    opencode-local-bridge
// @version      1.0.0
// @description  Explicit session transfer from Harness; never sends prompts or resumes goals.
// @match        https://chat.deepseek.com/*
// @inject-into  content
// @run-at       document-end
// @noframes
// @grant        GM.xmlHttpRequest
// @grant        GM.getValue
// @grant        GM.setValue
// @grant        GM.deleteValue
// @connect      127.0.0.1
// ==/UserScript==

(async () => {
  'use strict';
  if (window.top !== window || location.origin !== 'https://chat.deepseek.com') return;
  const key = 'deepseek-session-transfer-v1';
  // Replaced on export with the bridge PORT; an unexported copy uses 8000.
  const PORT_TEMPLATE = '__BRIDGE_PORT__';
  const validPort = value => /^[1-9][0-9]{0,4}$/.test(value) && Number(value) <= 65535;
  if (PORT_TEMPLATE !== '__BRIDGE_' + 'PORT__' && !validPort(PORT_TEMPLATE)) return;
  const PORT = validPort(PORT_TEMPLATE) ? PORT_TEMPLATE : '8000';
  let busy = false, stopped = false;
  const host = document.createElement('div');
  host.style.cssText = 'position:fixed;bottom:20px;right:20px;z-index:2147483647';
  const shadow = host.attachShadow({mode:'closed'});
  const label = document.createElement('div');
  label.style.cssText = 'background:white;color:#111;border:1px solid #888;border-radius:10px;padding:14px;max-width:380px;font:14px system-ui';
  shadow.append(label);
  function show(text) { label.textContent = text; if (!host.isConnected) document.documentElement.append(host); }
  async function takeFragment() {
    const params = new URLSearchParams(location.hash.slice(1));
    const nonce = params.get('bridge_session'), expires = Number(params.get('expires'));
    if (!nonce) return;
    history.replaceState(null, '', location.pathname + location.search);
    if (!/^[A-Za-z0-9_-]{43}$/.test(nonce) || !Number.isFinite(expires)
        || expires <= Date.now() || expires > Date.now()+610000) return;
    await GM.setValue(key, {nonce, expires});
    sessionStorage.setItem(key, nonce);
    stopped = false;
    show('Обновление сессии для Harness: войдите в тот же аккаунт DeepSeek.');
  }
  async function finish(text, nonce) {
    if (sessionStorage.getItem(key) !== nonce) return;
    stopped = true;
    const current = await GM.getValue(key);
    if (current?.nonce === nonce) await GM.deleteValue(key);
    sessionStorage.removeItem(key);
    show(text);
  }
  async function tick() {
    if (busy || stopped) return;
    const owner = sessionStorage.getItem(key);
    if (!owner) return;
    busy = true;
    try {
      const ticket = await GM.getValue(key);
      if (!ticket || ticket.nonce !== owner) return;
      if (Date.now() >= ticket.expires) { await finish('Время переноса истекло. Повторите /deepseek-login в Harness.', owner); return; }
      let token, account;
      try {
        token = JSON.parse(localStorage.getItem('userToken'))?.value;
        account = JSON.parse(localStorage.getItem('__appKit_userInfo'))?.value?.id;
      } catch (_) { return; }
      if (typeof token !== 'string' || !token || account == null) return;
      const digest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(token))))
        .map(x=>x.toString(16).padStart(2,'0')).join('');
      if (ticket.seen === digest) return;
      // Persist the attempt before transmission, including across navigation.
      ticket.seen = digest;
      await GM.setValue(key, ticket);
      const send = async sameAccount => {
        const r = await GM.xmlHttpRequest({url:'http://127.0.0.1:' + PORT + '/session/deepseek/capture',
          method:'POST', timeout:10000, headers:{Authorization:'Bearer '+ticket.nonce, 'Content-Type':'application/json'},
          data:JSON.stringify({token,account_id:String(account),user_agent:navigator.userAgent,cookie:document.cookie,same_account:sameAccount})});
        if (r.status !== 200) throw new Error('capture refused');
        return JSON.parse(r.responseText).status;
      };
      let status = await send(false);
      if (status === 'confirmation_required') {
        if (!window.confirm('Первое подключение Safari: это тот же аккаунт DeepSeek, что использовался в Harness? Подтверждение сохранит привязку истории.')) {
          await finish('Перенос отменён. Сессия моста не менялась.', owner); return;
        }
        status = await send(true);
      }
      if (status === 'new_login_required') {
        show('Этот токен уже отклонён. Выйдите и снова войдите в тот же аккаунт. Перенос продолжится после входа.'); return;
      }
      if (status !== 'saved') throw new Error('unexpected status');
      await finish('Сессия обновлена. История и счётчики сохранены. Цель в Harness не запускалась.', owner);
    } catch (_) {
      await finish('Перенос не завершён. Проверьте доступ Userscripts к 127.0.0.1 и /deepseek-login-status. Автоматических повторов нет.', owner);
    } finally { busy = false; }
  }
  await takeFragment();
  window.addEventListener('hashchange', () => { takeFragment().then(tick).catch(()=>show('Перенос не завершён.')); });
  await tick();
  // Local storage checks only; no polling of DeepSeek or bridge endpoints.
  setInterval(tick, 1000);
})();
