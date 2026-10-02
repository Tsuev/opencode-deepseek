// ==UserScript==
// @name         OpenCode browser response observer
// @namespace    opencode-local-bridge
// @version      0.2.4
// @description  Observe only the completion caused by an active local bridge job.
// @match        https://chat.z.ai/*
// @match        https://grok.com/*
// @match        https://chat.mistral.ai/*
// @match        https://www.kimi.com/*
// @match        https://www.kimi.ai/*
// @inject-into  page
// @run-at       document-start
// @weight       999
// @noframes
// @grant        none
// ==/UserScript==

(() => {
  "use strict";
  const CHANNEL = "opencode-local-response-v1";
  const LIMIT = 8 * 1024 * 1024;
  const completionPath = path => {
    if (location.hostname === "chat.z.ai") return path === "/api/chat/completions"
      || path === "/api/v2/chat/completions";
    if (location.hostname === "grok.com") return path === "/rest/app-chat/conversations/new"
      || /^\/rest\/app-chat\/conversations\/[\w-]+\/responses$/.test(path);
    if (["www.kimi.com", "www.kimi.ai"].includes(location.hostname)) return path === "/apiv2/kimi.gateway.chat.v1.ChatService/Chat";
    return location.hostname === "chat.mistral.ai";
  };
  let active = null;
  function retire() {
    if (active && window.fetch === active.fetch) window.fetch = active.restore;
    active = null;
  }
  document.addEventListener("opencode-local-job-v1", event => {
    let value;
    try { value = JSON.parse(event.detail); } catch (_) { return; }
    retire();
    if (!value || typeof value.nonce !== "string" || typeof value.prompt !== "string") return;
    const restore = window.fetch;
    active = { nonce: value.nonce, prompt: value.prompt, path: value.path, restore,
      observed: false, fetch: restore === earlyObserver ? earlyObserver : createObserver(restore.bind(window)) };
    window.fetch = active.fetch;
    document.dispatchEvent(new CustomEvent("opencode-local-ready-v1", {
      detail: JSON.stringify({ nonce: active.nonce, observing: window.fetch === active.fetch }),
    }));
  });

  async function observe(response, job, requestTurn) {
    const reader = response.body?.getReader();
    if (!reader) throw new Error("No completion response body");
    const parts = [];
    let size = 0;
    let envelope = new Uint8Array(0);
    const connect = ["www.kimi.com", "www.kimi.ai"].includes(location.hostname);
    while (true) {
      if (active?.nonce !== job.nonce) {
        void reader.cancel().catch(() => {});
        return;
      }
      const { done, value } = await reader.read();
      if (done) break;
      size += value.length;
      if (size > LIMIT) {
        void reader.cancel().catch(() => {});
        throw new Error("Completion response exceeded the size limit");
      }
      parts.push(value);
      if (connect) {
        const joined = new Uint8Array(envelope.length + value.length);
        joined.set(envelope); joined.set(value, envelope.length);
        envelope = joined;
        let offset = 0, ended = false;
        while (envelope.length - offset >= 5) {
          const flags = envelope[offset];
          const length = new DataView(envelope.buffer, envelope.byteOffset + offset + 1, 4).getUint32(0);
          if (length > LIMIT || (flags !== 0 && flags !== 2)) throw new Error("Unsupported Connect envelope");
          if (envelope.length - offset < length + 5) break;
          offset += length + 5;
          if (flags === 2) { ended = true; break; }
        }
        if (ended) {
          if (offset !== envelope.length) throw new Error("Data after Connect completion");
          // Connect's end envelope is the transport boundary. A frontend
          // wrapper may leave its cloned ReadableStream open afterward.
          void reader.cancel().catch(() => {});
          break;
        }
        envelope = envelope.slice(offset);
      }
    }
    if (active?.nonce !== job.nonce) return;
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const part of parts) { bytes.set(part, offset); offset += part.length; }
    let binary = "";
    for (let i = 0; i < bytes.length; i += 16384) {
      binary += String.fromCharCode(...bytes.subarray(i, i + 16384));
    }
    document.dispatchEvent(new CustomEvent(CHANNEL, { detail: JSON.stringify({
      nonce: job.nonce, status: response.status, body: btoa(binary),
      ...(requestTurn ? { request_turn: requestTurn } : {}),
    }) }));
  }

  function createObserver(nativeFetch) {
    return async function bridgeFetch(input, init) {
      const job = active;
      let candidate = null;
      if (job) {
        try {
          const request = new Request(input instanceof Request ? input.clone() : input, init);
          const url = new URL(request.url);
          if (request.method === "POST" && url.origin === location.origin && completionPath(url.pathname)) {
            // No request headers, cookies, tokens or unrelated responses are
            // read. The site's own request must contain this job's exact prompt.
            candidate = request.clone().text().then(body => {
              if (location.hostname === "chat.mistral.ai") {
                // Chat creation has no preallocated user-message identity.
                // Its wire bootstrap must bind the exact prompt instead.
                if (job.path === null) return body.includes(job.prompt)
                  || body.includes(JSON.stringify(job.prompt).slice(1, -1));
                const data = JSON.parse(body), chunks = data.messageInput;
                if (!Array.isArray(chunks) || !chunks.length || chunks.some(c => !c || c.type !== "text" || typeof c.text !== "string")
                    || chunks.map(c => c.text).join("") !== job.prompt
                    || typeof data.messageId !== "string" || !data.messageId
                    || (data.chatId !== undefined && (typeof data.chatId !== "string" || !data.chatId))) return null;
                return { user_id: data.messageId, version: 0, chat_id: data.chatId ?? null };
              }
              return body.includes(job.prompt) || body.includes(JSON.stringify(job.prompt).slice(1, -1));
            });
          }
        } catch (_) { /* Nonstandard requests are left untouched. */ }
      }
      const matches = candidate ? await candidate.catch(() => false) : false;
      const response = await nativeFetch(input, init);
      const compatibleResponse = location.hostname !== "chat.mistral.ai" || response.status !== 200
        || (response.headers.get("Content-Type") || "").includes("text/event-stream");
      if (matches && compatibleResponse && active === job && !job.observed) {
        // Early cached fetch and the per-job outer wrapper share one capture.
        job.observed = true;
        document.dispatchEvent(new CustomEvent(CHANNEL, { detail: JSON.stringify({ nonce: job.nonce, observing: true }) }));
        // Clone before returning: the frontend may immediately consume its own
        // body. Never await the clone's stream or block frontend rendering.
        void observe(response.clone(), job, location.hostname === "chat.mistral.ai" && typeof matches === "object" ? matches : null).catch(() => {
          document.dispatchEvent(new CustomEvent(CHANNEL, {
            detail: JSON.stringify({ nonce: job.nonce, error: "Completion observation failed" }),
          }));
        });
      }
      return response;
    };
  }
  // Frontends can cache fetch during startup; that reference must keep
  // observing future jobs even when the site later adds its own wrapper.
  const earlyObserver = createObserver(window.fetch.bind(window));
  window.fetch = earlyObserver;
})();
