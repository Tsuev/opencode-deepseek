// ==UserScript==
// @name         OpenCode signed-in tab controller
// @namespace    opencode-local-bridge
// @version      0.2.5
// @description  Opt-in local jobs in your existing signed-in browser tab.
// @match        https://chat.z.ai/*
// @match        https://grok.com/*
// @match        https://chat.mistral.ai/*
// @match        https://www.kimi.com/*
// @match        https://www.kimi.ai/*
// @inject-into  content
// @run-at       document-end
// @noframes
// @grant        GM.xmlHttpRequest
// @grant        GM.getValue
// @grant        GM.setValue
// ==/UserScript==

(async () => {
  "use strict";
  class BridgeFailure extends Error {}
  const TOKEN = "__BRIDGE_TOKEN__"; // Replaced only in ignored private exports.
  const BASE = "http://127.0.0.1:8000";
  const sites = {
    "chat.z.ai": { provider: "glm", home: "/", input: "#chat-input", path: /^\/c\/[\w-]+$/ },
    "grok.com": { provider: "grok", home: "/", input: 'form[data-composer=true] .query-bar-editor[contenteditable=true], form[data-composer=true] textarea[aria-label]', path: /^\/c\/[\w-]+$/ },
    "chat.mistral.ai": { provider: "mistral", home: "/work", input: ".ProseMirror[contenteditable=true]", path: /^\/(?:chat|work)\/[\w-]+$/ },
    "www.kimi.com": { provider: "kimi", home: "/", input: ".chat-input-editor[contenteditable=true]", path: /^\/chat\/[\w-]+$/ },
    "www.kimi.ai": { provider: "kimi", home: "/", input: ".chat-input-editor[contenteditable=true]", path: /^\/chat\/[\w-]+$/ },
  };
  const site = sites[location.hostname];
  if (!site || window.top !== window) return;
  const key = "opencode-bridge-tab-v1";
  let owner = sessionStorage.getItem(key);
  if (!owner) { owner = crypto.randomUUID(); sessionStorage.setItem(key, owner); }
  const activeKey = site.provider + ":" + owner;
  const documentId = crypto.randomUUID();
  let enabled = await GM.getValue(activeKey, false);
  let pending = null;
  let busy = false;
  let finishing = false;
  let timer;

  const badge = document.createElement("div");
  const shadow = badge.attachShadow({ mode: "closed" });
  badge.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483647";
  const button = document.createElement("button");
  button.style.cssText = "font:13px system-ui;padding:10px 14px;border:1px solid #777;border-radius:8px;background:#fff;color:#111;cursor:pointer";
  shadow.append(button);
  document.documentElement.append(badge);
  const label = message => { button.textContent = message || (enabled ? "OpenCode: подключено · отключить" : "Подключить вкладку к OpenCode"); };
  label();

  async function rpc(path, method = "GET", data) {
    const response = await GM.xmlHttpRequest({
      url: BASE + path, method, timeout: 10000,
      headers: { Authorization: "Bearer " + TOKEN, "Content-Type": "application/json" },
      ...(data ? { data: JSON.stringify(data) } : {}),
    });
    if (response.status !== 200) throw new BridgeFailure("Local bridge rejected the request");
    return JSON.parse(response.responseText);
  }

  const identity = job => ({provider:site.provider, id:job.id, owner, lease:job.lease, document:documentId});
  function retire() {
    pending = null;
    document.dispatchEvent(new CustomEvent("opencode-local-job-v1", { detail: "null" }));
    label();
  }

  async function finish(result) {
    const job = pending;
    retire();
    if (!job) return;
    finishing = true;
    try { await rpc("/browser/result", "POST", { ...identity(job), result }); }
    finally { finishing = false; }
  }

  button.addEventListener("click", async () => {
    if (TOKEN === "__BRIDGE_TOKEN__") { label("Сначала экспортируй приватные скрипты моста"); return; }
    button.title = "";
    enabled = !enabled;
    await GM.setValue(activeKey, enabled);
    if (!enabled && pending) await finish({ error: "Tab disconnected by user" }).catch(() => {});
    label();
    if (enabled) void poll();
  });

  const visible = element => !!element && element.getClientRects().length > 0
    && getComputedStyle(element).visibility === "visible" && !element.closest('[inert], [aria-hidden=true]');
  const editor = () => Array.from(document.querySelectorAll(site.input)).find(visible);
  const block = node => node.nodeType === Node.ELEMENT_NODE && /^(P|DIV|LI|PRE|BLOCKQUOTE)$/.test(node.tagName);
  function editorText(node) {
    if (node.nodeType === Node.TEXT_NODE) return node.textContent;
    if (node.nodeName === "BR") {
      // A lone BR is an empty paragraph's caret placeholder.
      return block(node.parentNode) && node.parentNode.childNodes.length === 1 ? "" : "\n";
    }
    let text = "", previous = null;
    for (const child of node.childNodes) {
      if (previous && (block(previous) || block(child))) text += "\n";
      text += editorText(child);
      previous = child;
    }
    return text;
  }
  const draft = input => input?.isContentEditable ? editorText(input) : input?.value;
  document.addEventListener("opencode-local-response-v1", async event => {
    let value;
    try { value = JSON.parse(event.detail); } catch (_) { return; }
    if (!pending || value?.nonce !== pending.nonce) return;
    if (value.observing === true) { button.title = "Receiving the matched site response"; return; }
    if (value.error) { await finish({ error: "Provider response could not be observed" }).catch(() => {}); return; }
    const job = pending;
    // Give the frontend a bounded chance to render its final answer and route.
    for (let attempt = 0; attempt < 50 && pending === job; attempt++) {
      if (site.path.test(location.pathname)) break;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    if (pending !== job) return;
    await finish({ status: value.status, body: value.body, path: location.pathname,
      ...(site.provider === "mistral" ? { request_turn: value.request_turn } : {}),
    }).catch(() => label("OpenCode: ответ не принят"));
  });

  async function execute(job) {
    if (!Number.isFinite(job.expires_in) || job.expires_in <= 0) throw new BridgeFailure("Invalid browser job deadline");
    const deadline = performance.now() + Math.min(job.expires_in, 1800) * 1000;
    let input = editor();
    if (draft(input)?.trim()) throw new BridgeFailure("The tab has an unsent draft");
    const target = job.path || site.home;
    if (target !== site.home && !site.path.test(target)) throw new BridgeFailure("Invalid conversation route");
    if (location.pathname !== target) {
      // Keep only a tab id and execution flag across navigation. No prompts,
      // response bodies or local pairing key are put in page storage.
      // The old document remains alive until navigation commits. Stop polling
      // now so it cannot repeatedly abort a slow navigation to the same route.
      await rpc("/browser/navigate", "POST", identity(job));
      clearInterval(timer);
      location.assign(location.origin + target);
      return;
    }
    // The extension can run before the SPA hydrates its signed-in editor.
    for (let attempt = 0; !input && enabled && attempt < 50; attempt++) {
      if (attempt % 5 === 0 && !(await rpc("/browser/check", "POST", identity(job))).active) {
        throw new BridgeFailure("The browser job was cancelled");
      }
      await new Promise(resolve => setTimeout(resolve, 100));
      input = editor();
    }
    if (!enabled || location.pathname !== target) throw new BridgeFailure("The tab changed before submission");
    if (!input) throw new BridgeFailure("The signed-in chat input is unavailable");
    if (draft(input)?.trim()) throw new BridgeFailure("The tab has an unsent draft");
    if (input.disabled || input.getAttribute("aria-disabled") === "true") throw new BridgeFailure("The chat input is busy");
    if (job.submitted) throw new BridgeFailure("The prompt was already submitted");
    pending = { ...job, nonce: crypto.randomUUID(), deadline };
    // Use strings across Safari's isolated/page worlds. Do not send upstream
    // until the observer confirms that this exact job can be captured.
    await new Promise((resolve, reject) => {
      const nonce = pending.nonce;
      const timeout = setTimeout(() => {
        document.removeEventListener("opencode-local-ready-v1", ready);
        reject(new BridgeFailure("Reload the tab to load the response observer"));
      }, 1500);
      function ready(event) {
        let value;
        try { value = JSON.parse(event.detail); } catch (_) { return; }
        if (value?.nonce !== nonce) return;
        clearTimeout(timeout);
        document.removeEventListener("opencode-local-ready-v1", ready);
        if (value.observing !== true) {
          reject(new BridgeFailure("The site replaced the response observer; reload the tab"));
          return;
        }
        resolve();
      }
      document.addEventListener("opencode-local-ready-v1", ready);
      document.dispatchEvent(new CustomEvent("opencode-local-job-v1", {
        detail: JSON.stringify({ nonce, prompt: job.prompt, path: job.path ?? null }),
      }));
    });
    await rpc("/browser/submit", "POST", identity(job));
    if (performance.now() >= deadline || !(await rpc("/browser/check", "POST", identity(job))).active
        || performance.now() >= deadline) {
      throw new BridgeFailure("The browser lease ended before insertion");
    }
    if (!enabled || pending?.id !== job.id) throw new BridgeFailure("The tab was disconnected before submission");
    if (location.pathname !== target || editor() !== input || draft(input)?.trim()) {
      throw new BridgeFailure("The editor changed before submission");
    }
    const originalDraft = draft(input);
    let insertedDraft, insertedMarkup, dispatching = false;
    try {
      input.focus();
      if (input.isContentEditable) {
        if (!document.execCommand("insertText", false, job.prompt)) throw new BridgeFailure("Chat editor did not accept the prompt");
      } else {
        const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
        if (!setter) throw new BridgeFailure("Unsupported chat input");
        setter.call(input, job.prompt);
        input.dispatchEvent(new Event("input", { bubbles: true }));
      }
      insertedDraft = draft(input);
      // Invoke the site's own send handler. The observer never constructs an
      // upstream request or touches the user's account credentials.
      if (site.provider !== "glm") {
        // Allow the frontend to commit the insertion before its send handler.
        await new Promise(resolve => setTimeout(resolve, 0));
        // Safari can expose the inserted text only on the next event-loop turn.
        // Rich editors also represent line breaks with DOM nodes, not text.
        const current = draft(input);
        if (current !== job.prompt) {
          throw new BridgeFailure("The editor did not retain the requested prompt");
        }
        insertedDraft = current;
        insertedMarkup = input.isContentEditable ? input.innerHTML : null;
        if (!(await rpc("/browser/check", "POST", identity(job))).active) {
          throw new BridgeFailure("The browser job was cancelled after insertion");
        }
        if (!enabled || pending?.id !== job.id) throw new BridgeFailure("The tab was disconnected after insertion");
        if (location.pathname !== target) throw new BridgeFailure("The conversation changed after insertion");
        if (editor() !== input) throw new BridgeFailure("The editor was replaced after insertion");
        if (draft(input) !== insertedDraft) throw new BridgeFailure("The editor draft changed after insertion");
        if (insertedMarkup !== null && input.innerHTML !== insertedMarkup) throw new BridgeFailure("The editor markup changed after insertion");
      }
      if (performance.now() >= deadline) throw new BridgeFailure("The browser lease expired before sending");
      if (site.provider === "grok") {
        const form = input.closest('form[data-composer=true]');
        if (!form) throw new BridgeFailure("The chat submission form is unavailable");
        dispatching = true;
        form.requestSubmit();
      } else {
        dispatching = true;
        input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true }));
      }
      label("OpenCode: запрос выполняется · отключить");
    } finally {
      // Remove only our unchanged insertion when no send handler was invoked.
      // Local cleanup must not depend on a cancelled lease accepting a result.
      if (!dispatching && insertedDraft !== undefined && input.isConnected
          && location.pathname === target && editor() === input && draft(input) === insertedDraft
          && (insertedMarkup === undefined || insertedMarkup === null || input.innerHTML === insertedMarkup)) {
        input.focus();
        if (input.isContentEditable) {
          const range = document.createRange();
          range.selectNodeContents(input);
          const selection = window.getSelection();
          selection.removeAllRanges();
          selection.addRange(range);
          document.execCommand(originalDraft ? "insertText" : "delete", false, originalDraft || null);
        } else {
          Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set.call(input, originalDraft);
          input.dispatchEvent(new Event("input", { bubbles: true }));
        }
      }
    }
  }

  async function poll() {
    if (busy || finishing || !enabled) return;
    busy = true;
    try {
      if (pending) {
        const job = pending;
        if (performance.now() >= job.deadline || !(await rpc("/browser/check", "POST", identity(job))).active) {
          if (pending === job) retire();
        }
        return;
      }
      const { job } = await rpc("/browser/jobs/" + site.provider + "?owner=" + encodeURIComponent(owner)
        + "&document=" + encodeURIComponent(documentId));
      if (!job) label();
      if (job) {
        try { await execute(job); }
        catch (error) {
          const reason = error instanceof BridgeFailure ? error.message : "Tab unavailable, reloaded, or contains an unsent draft";
          pending = { ...job };
          await finish({ error: reason }).catch(() => {});
          enabled = false;
          await GM.setValue(activeKey, false);
          label("OpenCode: остановлено · подключить");
          button.title = reason;
        }
      }
    } catch (_) { label("OpenCode: локальный мост недоступен"); }
    finally { busy = false; }
  }
  timer = setInterval(() => void poll(), 500);
  window.addEventListener("pagehide", () => clearInterval(timer), { once: true });
  if (enabled && TOKEN !== "__BRIDGE_TOKEN__") void poll();
})();
