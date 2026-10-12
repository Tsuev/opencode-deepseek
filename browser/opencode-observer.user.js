// ==UserScript==
// @name         OpenCode browser response observer
// @namespace    opencode-local-bridge
// @version      0.3.17
// @description  Observe only the completion caused by an active local bridge job.
// @match        https://chat.z.ai/*
// @match        https://grok.com/*
// @match        https://chat.mistral.ai/*
// @match        https://www.kimi.com/*
// @match        https://www.kimi.ai/*
// @match        https://chat.qwen.ai/*
// @match        https://chatgpt.com/*
// @match        https://chat.openai.com/*
// @match        https://claude.ai/*
// @match        https://gemini.google.com/*
// @match        https://www.google.com/*
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
  // One row per host. `stalls` means the UI can finish while the HTTP body stays
  // open (fetch watch + XHR). Kimi is Connect RPC and is not in that set.
  // `fullTail` keeps the whole body for done-detection; others keep a 256-char tail.
  const pages = {
    "chat.z.ai": { provider: "glm" },
    "grok.com": { provider: "grok", stalls: true, fullTail: true, socket: "grok" },
    "chat.mistral.ai": { provider: "mistral", stalls: true, fullTail: true, socket: "mistral" },
    "www.kimi.com": { provider: "kimi", connect: true },
    "www.kimi.ai": { provider: "kimi", connect: true },
    "chat.qwen.ai": { provider: "qwen", stalls: true },
    "chatgpt.com": { provider: "chatgpt", stalls: true, fullTail: true },
    "chat.openai.com": { provider: "chatgpt", stalls: true, fullTail: true },
    // Claude wire paths drift; capture same-origin /api/* and fall back to DOM scrape.
    "claude.ai": { provider: "claude", stalls: true, fullTail: true },
    "gemini.google.com": { provider: "gemini", stalls: true, fullTail: true },
    "www.google.com": { provider: "gemini", stalls: true, fullTail: true },
  };
  const page = pages[location.hostname] || {};
  const geminiPage = page.provider === "gemini";
  const chatgptPage = page.provider === "chatgpt";
  const claudePage = page.provider === "claude";
  const geminiHost = hostname => ["gemini.google.com", "www.google.com", "google.com"].includes(hostname);
  // Completion URLs we may capture. gemini.google.com: BardChatUi /
  // StreamGenerate / batchexecute (closer to other providers' wire text).
  // AI Mode (udm=50): /async/folif HTML shells — observer still waits for EOF
  // and rejects prompt-echo, but the controller usually finishes via DOM
  // scrape instead of trusting this body. Why: browser_protocol.py note above
  // gemini_answer ("Gemini browser answer capture").
  const geminiCompletion = url => {
    if (!geminiHost(url.hostname)) return false;
    const path = url.pathname || "";
    if (/\/_\/BardChatUi\//i.test(path)) return true;
    if (/StreamGenerate/i.test(path + (url.search || ""))) return true;
    if (/\/batchexecute\/?$/i.test(path)) return true;
    if (path === "/async/folif" || path === "/async/folwr") return true;
    return false;
  };
  const requestUrl = (input, init) => {
    try {
      if (typeof input === "string") return new URL(input, location.href);
      if (input && typeof input.url === "string") return new URL(input.url, location.href);
    } catch (_) { /* ignore */ }
    return null;
  };
  const requestMethod = (input, init) => {
    const value = (init && init.method) || (input && input.method) || "GET";
    return String(value || "GET").toUpperCase();
  };
  // Exact stream endpoints only. startsWith would also match JSON helpers like
  // /f/conversation/prepare and /finalize, which steal the exclusive capture.
  const chatgptCompletion = path => path === "/backend-api/conversation"
    || path === "/backend-api/f/conversation"
    || path === "/backend-api/conversation/resume"
    || path === "/backend-api/f/conversation/resume";
  const completionPath = (path, url) => {
    if (page.provider === "glm") return path === "/api/chat/completions"
      || path === "/api/v2/chat/completions";
    if (page.provider === "grok") return path === "/rest/app-chat/conversations/new"
      || /^\/rest\/app-chat\/conversations\/[\w-]+\/responses$/.test(path);
    if (page.provider === "kimi") return path === "/apiv2/kimi.gateway.chat.v1.ChatService/Chat";
    if (page.provider === "qwen") {
      return path === "/api/v2/chat/completions" || path.startsWith("/api/v2/chat/completions/");
    }
    if (page.provider === "chatgpt") return chatgptCompletion(path);
    if (page.provider === "gemini" && url) return geminiCompletion(url);
    // Claude.ai completion SSE only — not create-conversation / org helpers.
    if (page.provider === "claude") {
      return /\/chat_conversations\/[\w-]+\/(?:completion|retry_completion)\/?$/.test(path);
    }
    return page.provider === "mistral";
  };
  const claudeCompletionHead = text => /content_block_delta|"type"\s*:\s*"completion"|message_stop/.test(text);
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
      observed: false, completed: false, geminiProbe: false,
      fetch: restore === earlyObserver ? earlyObserver : createObserver(restore.bind(window)) };
    window.fetch = active.fetch;
    document.dispatchEvent(new CustomEvent("opencode-local-ready-v1", {
      detail: JSON.stringify({ nonce: active.nonce, observing: window.fetch === active.fetch }),
    }));
  });
  // Controller rejected a prompt-echo body; allow a later completion to win.
  document.addEventListener("opencode-gemini-reopen-v1", event => {
    let value;
    try { value = JSON.parse(event.detail); } catch (_) { return; }
    if (!active || !geminiPage || value?.nonce !== active.nonce) return;
    active.observed = false;
    active.completed = false;
  });

  const sseDone = text => /(?:^|\r?\n)data:\s*\[DONE\]\s*(?:\r?\n|$)/.test(text);
  // Gemini batchexecute streams often end with an [["e", ...]] trailer.
  // AI Mode /async/folif has no that marker; observe() waits for HTTP EOF.
  const geminiDone = text => /\[\s*\[\s*"e"\s*,/.test(text);
  // Lock capture only once the body looks like a real chat stream, not a
  // tiny BardChatUi helper RPC that also hits /batchexecute.
  const geminiCompletionHead = text => {
    const head = String(text || "");
    if (!head) return false;
    if (head.includes("wrb.fr") || head.includes("BardErrorInfo") || head.includes(")]}'")) return true;
    if (head.includes("StreamGenerate")) return true;
    // AI Mode HTML fragments are not wrb.fr; accept substantive markup.
    if (head.includes("<") && head.length > 128) return true;
    return false;
  };
  // AI Mode /async/folif often finishes an early shell that only restates the
  // pasted OpenCode prompt (tools schema) while the UI keeps streaming later.
  // Treat those bodies as non-answers so the exclusive job can wait.
  const geminiPlainText = text => String(text || "")
    .replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/gi, " ")
    .replace(/&quot;/gi, "\"")
    .replace(/&#39;|&apos;/gi, "'")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&amp;/gi, "&")
    .replace(/\s+/g, " ")
    .trim();
  const geminiHasOpenCodeTools = text => text.includes("Available tools, given as JSON Schema")
    || (text.includes("```tool_calls") && text.includes("You can call functions"));
  const geminiPromptEchoOnly = (raw, prompt) => {
    const got = geminiPlainText(raw);
    const wanted = geminiPlainText(prompt);
    if (!got) return true;
    if (wanted) {
      if (got === wanted) return true;
      if (wanted.length >= 64 && wanted.startsWith(got) && got.length >= 64) return true;
      if (got.startsWith(wanted) && got.length <= wanted.length + 96) return true;
    }
    const hasTools = geminiHasOpenCodeTools(got);
    const head = wanted && wanted.length >= 48 ? wanted.slice(0, 80) : "";
    if (!hasTools && !(head && got.includes(head))) return false;
    // OpenCode tool instructions in the body mean AI Mode restated our paste.
    // Only accept when the full prompt is present and a non-empty remainder remains
    // (the model answer). Otherwise keep waiting — early folif shells often end
    // ~10s with tools/prompt and no real answer yet.
    if (hasTools && !(wanted && got.includes(wanted))) return true;
    if (wanted && got.includes(wanted)) {
      const rest = got.split(wanted).join(" ").replace(/\s+/g, " ").trim();
      return !rest || geminiHasOpenCodeTools(rest);
    }
    return true;
  };
  const geminiMarkProbe = job => {
    if (!job || job.geminiProbe) return;
    job.geminiProbe = true;
    // Soft signal for the controller's "not observed" timeout; exclusive lock
    // (job.observed) waits until a non-echo body is ready to emit.
    document.dispatchEvent(new CustomEvent(CHANNEL, {
      detail: JSON.stringify({ nonce: job.nonce, observing: true }),
    }));
  };
  // Mistral Vibe: prefer 8:null; UI is done at generationStatus success and the
  // socket may stay open afterward (same hang class as Grok).
  const mistralDone = text => /(?:^|\r?\n)8:null\s*(?:\r?\n|$)/.test(text)
    || /"generationStatus"\s*:\s*"success"/.test(text)
    || /"path"\s*:\s*"\/generationStatus"[\s\S]{0,80}"value"\s*:\s*"success"/.test(text);
  const mistralCompletionHead = text => {
    const head = String(text || "").trimStart().slice(0, 16384);
    if (!head) return false;
    if (head.startsWith("15:") || head.startsWith("data:")) return true;
    if (head.includes("\n15:") || head.includes("\ndata:")) return true;
    if (/"type"\s*:\s*"(bootstrap|message)"/.test(head)) return true;
    if (/generationStatus|messageInput|contentChunks/.test(head)) return true;
    // Next.js flight often emits other row codes before the 15: patches.
    if (/^\d+:/.test(head) && (head.includes("15:") || head.includes("bootstrap"))) return true;
    return false;
  };
  // Grok NDJSON: UI finishes when a non-partial modelResponse arrives; the
  // HTTP body may stay open afterward (same class of hang as Qwen/Gemini).
  // Align with grok_answer: omit/false partial is terminal; only partial===true waits.
  const grokDone = text => {
    for (const line of String(text || "").split(/\r?\n/)) {
      const row = line.trim();
      if (!row.startsWith("{")) continue;
      try {
        const frame = JSON.parse(row);
        const result = frame && frame.result;
        if (!result || typeof result !== "object") continue;
        const response = result.response && typeof result.response === "object"
          ? result.response : result;
        const model = response.modelResponse;
        if (model && typeof model === "object" && model.partial !== true
            && typeof model.message === "string" && model.message
            && !model.error && !model.streamErrors) return true;
      } catch (_) { /* incomplete NDJSON line */ }
    }
    return false;
  };
  const streamDone = text => {
    if (page.provider === "gemini") return geminiDone(text);
    if (page.provider === "grok") return grokDone(text);
    if (page.provider === "mistral") return mistralDone(text) || sseDone(text);
    if (page.provider === "claude") {
      // Conversation JSON dump, Anthropic-style stop, or generic SSE [DONE].
      // Do NOT treat `"stop_reason": null` in message_start as done — that fires
      // while Claude is still thinking and truncates the capture.
      if (sseDone(text)) return true;
      try {
        const payload = JSON.parse(text);
        if (payload && typeof payload === "object" && Array.isArray(payload.chat_messages)
            && payload.chat_messages.some(m => m && m.sender === "assistant"
              && (typeof m.text === "string" ? m.text : "").trim())) {
          return true;
        }
      } catch (_) { /* stream chunk */ }
      return /"type"\s*:\s*"message_stop"/.test(text)
        || /"stop_reason"\s*:\s*"(?:end_turn|max_tokens|stop_sequence|tool_use)"/.test(text);
    }
    return sseDone(text);
  };
  const geminiAppPath = () => {
    const match = location.pathname.match(/^\/(?:u\/\d+\/)?app\/([\w-]+)$/);
    return match ? "/app/" + match[1] : null;
  };
  const emitBody = (job, status, binary, conversationPath, requestTurn) => {
    if (active?.nonce !== job.nonce) return;
    let encoded = "";
    for (let i = 0; i < binary.length; i += 16384) {
      encoded += String.fromCharCode(...binary.subarray(i, i + 16384));
    }
    document.dispatchEvent(new CustomEvent(CHANNEL, { detail: JSON.stringify({
      nonce: job.nonce, status, body: btoa(encoded),
      ...(typeof conversationPath === "string" ? { path: conversationPath } : {}),
      ...(requestTurn ? { request_turn: requestTurn } : {}),
    }) }));
  };
  const beginCapture = job => {
    if (!job || job.observed || job.completed || active !== job) return false;
    job.observed = true;
    document.dispatchEvent(new CustomEvent(CHANNEL, {
      detail: JSON.stringify({ nonce: job.nonce, observing: true }),
    }));
    return true;
  };
  const failCapture = job => {
    document.dispatchEvent(new CustomEvent(CHANNEL, {
      detail: JSON.stringify({ nonce: job.nonce, error: "Completion observation failed" }),
    }));
  };

  async function observe(response, job, requestTurn, conversationPath, options = {}) {
    const reader = response.body?.getReader();
    if (!reader) throw new Error("No completion response body");
    const parts = [];
    let size = 0;
    let envelope = new Uint8Array(0);
    let sseTail = "";
    let probe = "";
    let began = !options.deferBegin;
    const connect = !!page.connect;
    // Qwen/Gemini/Grok/Mistral/ChatGPT may leave the HTTP body open after the UI finishes.
    const watchStream = !!page.stalls;
    const decoder = (watchStream || options.deferBegin) ? new TextDecoder() : null;
    if (!options.deferBegin && !beginCapture(job)) {
      void reader.cancel().catch(() => {});
      return;
    }
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
      if (!began) {
        probe += decoder.decode(value, { stream: true });
        const sseType = (options.contentType || "").toLowerCase().includes("text/event-stream");
        if (mistralCompletionHead(probe) || geminiCompletionHead(probe)
            || (claudePage && claudeCompletionHead(probe))
            || (sseType && probe.length > 0)) {
          if (geminiPage) {
            // Do not exclusive-lock yet: an early folif shell may be prompt-only.
            geminiMarkProbe(job);
            began = true;
            sseTail = probe;
            if (streamDone(sseTail)) {
              void reader.cancel().catch(() => {});
              break;
            }
            continue;
          }
          if (!beginCapture(job)) {
            void reader.cancel().catch(() => {});
            return;
          }
          began = true;
          sseTail = probe;
          if (streamDone(sseTail)) {
            void reader.cancel().catch(() => {});
            break;
          }
          continue;
        }
        // Keep reading: 15: rows may follow other Next flight prefixes.
        if (size < 16384) continue;
        // Wrong same-origin POST; leave the job open for the real stream.
        void reader.cancel().catch(() => {});
        return;
      }
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
      } else if (watchStream) {
        const chunk = decoder.decode(value, { stream: true });
        // Grok NDJSON lines and Mistral 15: patch rows can exceed a 256-char window.
        sseTail = page.fullTail ? sseTail + chunk : (sseTail + chunk).slice(-256);
        if (streamDone(sseTail)) {
          void reader.cancel().catch(() => {});
          break;
        }
      }
    }
    if (!began || active?.nonce !== job.nonce) return;
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const part of parts) { bytes.set(part, offset); offset += part.length; }
    if (geminiPage) {
      const text = new TextDecoder().decode(bytes);
      if (geminiPromptEchoOnly(text, job.prompt)) {
        // Keep the job open for a later completion that carries the answer.
        return;
      }
      if (!beginCapture(job)) return;
      job.completed = true;
    }
    emitBody(job, response.status, bytes, conversationPath, requestTurn);
  }

  const qwenChatPath = (url, body) => {
    const fromQuery = url.searchParams.get("chat_id");
    if (typeof fromQuery === "string" && /^[a-zA-Z0-9_-]{1,128}$/.test(fromQuery)) return "/c/" + fromQuery;
    try {
      const data = JSON.parse(body);
      const id = data && typeof data.chat_id === "string" ? data.chat_id : null;
      if (id && /^[a-zA-Z0-9_-]{1,128}$/.test(id)) return "/c/" + id;
    } catch (_) { /* non-JSON bodies are matched by substring only */ }
    return null;
  };
  const norm = value => (value || "").replace(/\r\n/g, "\n");
  const qwenPromptMatch = (body, prompt) => {
    const wanted = norm(prompt);
    if (!wanted) return false;
    if (norm(body).includes(wanted) || body.includes(JSON.stringify(prompt).slice(1, -1))) return true;
    try {
      const data = JSON.parse(body);
      const messages = data && data.messages;
      if (!Array.isArray(messages)) return false;
      for (const message of messages) {
        if (!message || message.role !== "user") continue;
        let text = message.content;
        if (Array.isArray(text)) {
          text = text
            .filter(part => part && part.type === "text" && typeof part.text === "string")
            .map(part => part.text).join("");
        }
        if (typeof text !== "string") continue;
        text = norm(text);
        if (text === wanted || text.includes(wanted) || (wanted.includes(text) && text.length >= 32)) return true;
        // Long OpenCode prompts: match a stable tail if the site truncates the head.
        if (wanted.length > 64) {
          const tail = wanted.slice(-96);
          const head = wanted.slice(0, 64);
          if (text.includes(tail) || (text.includes(head) && text.length >= 64)) return true;
        }
      }
    } catch (_) { /* fall through */ }
    return false;
  };
  const qwenRequestBody = body => {
    if (typeof body === "string") return body;
    if (body == null) return "";
    try {
      if (typeof Uint8Array !== "undefined" && body instanceof Uint8Array) {
        return new TextDecoder().decode(body);
      }
      if (typeof ArrayBuffer !== "undefined" && body instanceof ArrayBuffer) {
        return new TextDecoder().decode(body);
      }
    } catch (_) { /* ignore */ }
    return "";
  };
  // Exclusive connected Grok tab: capture by completion path only. The SPA may
  // preallocate a conversation id and POST /responses even for a new job
  // (path=null), and wire prompt fields often diverge from the editor text.
  const bindGrok = (url, job) => {
    const isNew = url.pathname === "/rest/app-chat/conversations/new";
    const match = url.pathname.match(/^\/rest\/app-chat\/conversations\/([\w-]+)\/responses$/);
    if (!isNew && !match) return false;
    const chatId = match ? match[1] : null;
    if (job.path !== null) {
      if (!chatId || job.path !== "/c/" + chatId) return false;
    }
    return { chat_id: chatId, prompt: job.prompt, endpoint: url.pathname };
  };
  const mistralChatId = (job, chatId) => {
    if (job.path === null) return chatId ?? null;
    const expected = job.path.replace(/^\/(?:chat|work|code)\//, "");
    if (chatId != null && chatId !== expected) return false;
    return chatId ?? expected;
  };
  // When messageInput is present it must match this job. Opaque/FormData bodies
  // on the exclusive tab return true; observe() then probes the response head.
  const bindMistral = (body, job) => {
    try {
      const data = JSON.parse(body);
      if (data && typeof data === "object" && !Array.isArray(data)) {
        const chunks = data.messageInput;
        if (Array.isArray(chunks) && chunks.length) {
          if (chunks.some(c => !c || c.type !== "text" || typeof c.text !== "string")
              || typeof data.messageId !== "string" || !data.messageId
              || (data.chatId !== undefined && data.chatId !== null && typeof data.chatId !== "string")) {
            return false;
          }
          const text = norm(chunks.map(c => c.text).join(""));
          const wanted = norm(job.prompt);
          const matched = text === wanted
            || (wanted.length > 64 && (text.includes(wanted) || wanted.includes(text)));
          const chatId = mistralChatId(job, data.chatId ?? null);
          if (chatId === false) return false;
          // Rewritten wire text: still observe on exclusive tab; bootstrap binds prompt.
          if (!matched) return true;
          return { user_id: data.messageId, version: 0, chat_id: chatId };
        }
        if (typeof data.messageId === "string" && data.messageId) {
          const chatId = mistralChatId(job, typeof data.chatId === "string" ? data.chatId : null);
          if (chatId === false) return false;
          return { user_id: data.messageId, version: 0, chat_id: chatId };
        }
      }
    } catch (_) { /* FormData / flight bodies */ }
    return true;
  };
  // Exclusive connected ChatGPT tab: capture streamed conversation turns only.
  // Prefer conversation_id from the request; SPA path is a fallback.
  const chatgptChatPath = (body, job) => {
    try {
      const data = JSON.parse(body);
      if (!data || typeof data !== "object" || Array.isArray(data)) return false;
      // Prepare/finalize helpers may include draft text but are not the SSE turn.
      if (data.action == null && !Array.isArray(data.messages)) return false;
      const cid = data.conversation_id;
      if (typeof cid === "string" && /^[\w-]{1,128}$/.test(cid)) {
        if (job.path !== null && job.path !== "/c/" + cid) return false;
        return { path: "/c/" + cid };
      }
    } catch (_) { /* opaque / compressed bodies */ }
    if (job.path !== null) return { path: job.path };
    const match = location.pathname.match(/^\/c\/([\w-]+)$/);
    return { path: match ? "/c/" + match[1] : null };
  };
  // GLM still requires an exact last-user-message prompt binding.
  const bindGlm = (url, body, job) => {
    let data;
    try { data = JSON.parse(body); } catch (_) { return false; }
    if (!data || typeof data !== "object" || Array.isArray(data)) return false;
    const candidates = [];
    if (typeof data.message === "string") candidates.push(data.message);
    if (typeof data.query === "string") candidates.push(data.query);
    if (Array.isArray(data.messages) && data.messages.length) {
      const last = data.messages[data.messages.length - 1];
      if (last?.role !== "user" || typeof last.content !== "string") return false;
      candidates.push(last.content);
    }
    const wanted = norm(job.prompt);
    if (!candidates.some(text => {
      const got = norm(text);
      return got === wanted
        || (wanted.length > 64 && (got.includes(wanted) || wanted.includes(got)));
    })) return false;
    const ids = [data.chat_id, data.chatId].filter(id => id !== undefined && id !== null && id !== "");
    if (ids.some(id => typeof id !== "string") || new Set(ids).size > 1) return false;
    const chatId = ids.length ? ids[0] : null;
    if (job.path !== null && job.path !== "/c/" + chatId) return false;
    return { chat_id: chatId, prompt: job.prompt, endpoint: url.pathname };
  };

  // Exclusive connected Gemini tab: capture by completion URL. The SPA often
  // protobuf-escapes the prompt so substring matching is unreliable, and the
  // request body may be a one-shot stream that cannot be cloned for reading.
  function bindGemini(url) {
    return { path: geminiAppPath() };
  }

  // Claude: prefer conv id from /chat_conversations/<id>/completion URL.
  const claudeChatPath = (url, body, job) => {
    const fromUrl = String(url.pathname || "").match(
      /\/chat_conversations\/([\w-]+)\/(?:completion|retry_completion)\/?$/,
    );
    if (fromUrl) {
      const path = "/chat/" + fromUrl[1];
      if (job.path !== null && job.path !== path) return false;
      return { path };
    }
    try {
      const data = JSON.parse(body);
      if (data && typeof data === "object" && !Array.isArray(data)) {
        const cid = data.conversation_uuid || data.chat_id || data.uuid
          || (data.conversation && data.conversation.uuid);
        if (typeof cid === "string" && /^[\w-]{1,128}$/.test(cid)) {
          const path = "/chat/" + cid;
          if (job.path !== null && job.path !== path) return false;
          return { path };
        }
      }
    } catch (_) { /* opaque / stream bodies */ }
    if (job.path !== null) return { path: job.path };
    const match = location.pathname.match(/^\/chat\/([\w-]+)$/);
    return { path: match ? "/chat/" + match[1] : null };
  };

  // Shared by fetch and XHR. Strict binders (GLM, Grok, Mistral) stay separate
  // from exclusive-tab path recovery (Qwen, ChatGPT, Gemini, Claude).
  function bindCompletion(url, body, job, xhr) {
    if (page.provider === "mistral") return bindMistral(body, job);
    if (page.provider === "qwen") return { path: qwenChatPath(url, body) };
    if (page.provider === "chatgpt") return chatgptChatPath(body, job);
    if (page.provider === "claude") return claudeChatPath(url, body, job);
    if (page.provider === "gemini") return bindGemini(url);
    if (page.provider === "grok") return bindGrok(url, job);
    if (page.provider === "glm") return bindGlm(url, body, job);
    return body.includes(job.prompt) || body.includes(JSON.stringify(job.prompt).slice(1, -1));
  }

  function splitBound(bound) {
    if (!bound || typeof bound !== "object") return { requestTurn: null, conversationPath: null };
    if ("path" in bound && !("endpoint" in bound) && !("user_id" in bound)) {
      return { requestTurn: null, conversationPath: bound.path };
    }
    return { requestTurn: bound, conversationPath: null };
  }

  function createObserver(nativeFetch) {
    return async function bridgeFetch(input, init) {
      const job = active;
      let candidate = null;
      if (job) {
        try {
          // Resolve URL/method without cloning stream bodies (Gemini may POST
          // with duplex streams that throw on `new Request(...)` without duplex).
          const url = requestUrl(input, init);
          const method = requestMethod(input, init);
          if (url) {
            const sameOrigin = url.origin === location.origin;
            // Gemini on www.google.com often posts to gemini.google.com (cross-origin).
            const allowed = sameOrigin || (geminiPage && geminiCompletion(url));
            const mistralGet = page.provider === "mistral" && method === "GET";
            // AI Mode may fetch /async/folif with GET; Gemini chat POSTs BardChatUi.
            const geminiGet = geminiPage && method === "GET" && geminiCompletion(url);
            if ((method === "POST" || mistralGet || geminiGet) && allowed && completionPath(url.pathname, url)) {
              if (mistralGet) {
                candidate = Promise.resolve(true);
              } else if (page.provider === "gemini") {
                candidate = Promise.resolve(bindGemini(url));
              } else {
                candidate = Promise.resolve().then(async () => {
                  const request = new Request(
                    input instanceof Request ? input.clone() : input, init,
                  );
                  return bindCompletion(url, await request.clone().text(), job, false);
                });
              }
            }
          }
        } catch (_) { /* Nonstandard requests are left untouched. */ }
      }
        let matches = candidate
        ? await candidate.catch(() => (
          page.provider === "mistral" || page.provider === "gemini" || claudePage
        ))
        : false;
      const response = await nativeFetch(input, init);
      // Vibe streams patch rows under varying Content-Types; exclusive tab only
      // requires HTTP 200 and a completion-shaped body (probed in observe).
      const contentType = response.headers.get("Content-Type") || "";
      const compatibleResponse = page.provider !== "mistral" || response.status === 200;
      // ChatGPT/Claude: never lock onto application/json helpers — only SSE completions.
      if (matches && (chatgptPage || claudePage)
          && !contentType.toLowerCase().includes("event-stream")) {
        matches = false;
      }
      // Gemini: allow parallel candidate streams until one non-echo body wins.
      if (matches && compatibleResponse && active === job
          && !(geminiPage ? job.completed : job.observed)) {
        // Clone before returning: the frontend may immediately consume its own
        // body. Never await the clone's stream or block frontend rendering.
        const split = splitBound(matches);
        const requestTurn = split.requestTurn;
        const conversationPath = split.conversationPath;
        // Gemini batchexecute is noisy; probe for wrb.fr / HTML before locking.
        const deferBegin = page.provider === "mistral" || chatgptPage || geminiPage || claudePage;
        let clone = null;
        try { clone = response.clone(); } catch (_) { clone = null; }
        if (clone) {
          void observe(clone, job, requestTurn, conversationPath, {
            deferBegin,
            contentType,
          }).catch(() => {
            if (job.observed) failCapture(job);
          });
        }
      }
      return response;
    };
  }

  // Sites that leave the body open may also complete over XHR instead of fetch.
  if (page.stalls && typeof XMLHttpRequest !== "undefined") {
    const proto = XMLHttpRequest.prototype;
    const nativeOpen = proto.open;
    const nativeSend = proto.send;
    proto.open = function(method, url, ...rest) {
      try {
        this.__opencodeBridge = { method: String(method || ""), url: String(url || "") };
      } catch (_) {
        this.__opencodeBridge = null;
      }
      return nativeOpen.apply(this, [method, url, ...rest]);
    };
    proto.send = function(body) {
      const job = active;
      const meta = this.__opencodeBridge;
      let conversationPath = null;
      let requestTurn = null;
      let track = false;
      if (job && meta && /^(POST|GET)$/i.test(meta.method)) {
        try {
          const url = new URL(meta.url, location.href);
          const sameOrigin = url.origin === location.origin;
          const allowed = sameOrigin || (geminiPage && geminiCompletion(url));
          const method = String(meta.method || "").toUpperCase();
          if (method === "GET" && !(page.provider === "mistral" || (geminiPage && geminiCompletion(url)))) {
            /* only Mistral/Gemini AI Mode use GET completions */
          } else if (allowed && completionPath(url.pathname, url)) {
            const bound = page.provider === "gemini"
              ? bindGemini(url)
              : bindCompletion(url, method === "GET" ? "" : qwenRequestBody(body), job, true);
            track = !!bound;
            const split = splitBound(bound);
            requestTurn = split.requestTurn;
            conversationPath = split.conversationPath;
          }
        } catch (_) { /* leave the site request untouched */ }
      }
      // Mistral/Gemini: many same-origin POSTs share the completion host; probe
      // the response head before locking the exclusive capture slot.
      const deferXhr = page.provider === "mistral" || geminiPage;
      if (track && (geminiPage ? !job.completed : (deferXhr ? !job.observed : beginCapture(job)))) {
        let finished = false;
        const arm = () => {
          if (job.completed) return false;
          if (job.observed && !geminiPage) return true;
          if (!deferXhr) return false;
          const text = String(this.responseText || "");
          if (page.provider === "mistral" && !mistralCompletionHead(text)) return false;
          if (geminiPage && !geminiCompletionHead(text)) return false;
          if (geminiPage) {
            geminiMarkProbe(job);
            return true;
          }
          return beginCapture(job);
        };
        const complete = () => {
          if (finished || active?.nonce !== job.nonce) return;
          if (!arm()) return;
          try {
            const text = String(this.responseText || "");
            if (geminiPage && geminiPromptEchoOnly(text, job.prompt)) return;
            if (geminiPage) {
              if (!beginCapture(job)) return;
              job.completed = true;
            }
            finished = true;
            if (text.length > LIMIT) throw new Error("Completion response exceeded the size limit");
            const bytes = new TextEncoder().encode(text);
            emitBody(job, this.status || 0, bytes, conversationPath, requestTurn);
          } catch (_) {
            failCapture(job);
          }
        };
        this.addEventListener("progress", () => {
          if (!arm()) return;
          if (streamDone(String(this.responseText || ""))) {
            complete();
            try { this.abort(); } catch (_) { /* already closing */ }
          }
        });
        this.addEventListener("load", complete);
        this.addEventListener("error", () => {
          if (!finished && job.observed) failCapture(job);
        });
        this.addEventListener("abort", () => {
          if (finished) return;
          if (job.observed && streamDone(String(this.responseText || ""))) complete();
          else if (job.observed) failCapture(job);
        });
      }
      return nativeSend.apply(this, arguments);
    };
  }

  // Long-lived WebSockets bypass fetch/XHR. Grok uses mgw; Mistral/Vibe may too.
  if (page.socket && typeof WebSocket !== "undefined") {
    const nativeWsSend = WebSocket.prototype.send;
    const wsDecode = data => {
      if (typeof data === "string") return data;
      try {
        if (typeof ArrayBuffer !== "undefined" && data instanceof ArrayBuffer) {
          return new TextDecoder().decode(data);
        }
        if (ArrayBuffer.isView(data)) return new TextDecoder().decode(data);
      } catch (_) { /* binary frames left untouched */ }
      return "";
    };
    const watchMistralSocket = (socket, job) => {
      const chunks = [];
      let finished = false;
      const onMessage = event => {
        if (finished || active?.nonce !== job.nonce) {
          socket.removeEventListener("message", onMessage);
          return;
        }
        const raw = typeof event.data === "string" ? event.data : wsDecode(event.data);
        if (!raw) return;
        chunks.push(raw);
        const all = chunks.join("\n");
        if (all.length > LIMIT) {
          finished = true;
          socket.removeEventListener("message", onMessage);
          failCapture(job);
          return;
        }
        if (!(mistralDone(all) || sseDone(all))) return;
        finished = true;
        socket.removeEventListener("message", onMessage);
        try {
          if (!mistralCompletionHead(all) && !all.includes("15:")) {
            failCapture(job);
            return;
          }
          const bytes = new TextEncoder().encode(all);
          emitBody(job, 200, bytes, null, null);
        } catch (_) {
          failCapture(job);
        }
      };
      socket.addEventListener("message", onMessage);
    };
    const watchGrokSocket = (socket, job) => {
      const state = { texts: [], chatId: null, size: 0, finished: false };
      const finish = (ok, message) => {
        if (state.finished || active?.nonce !== job.nonce) return;
        state.finished = true;
        socket.removeEventListener("message", onMessage);
        if (!ok) { failCapture(job); return; }
        let chatId = state.chatId;
        if (!chatId && typeof job.path === "string" && job.path.startsWith("/c/")) {
          chatId = job.path.slice(3);
        }
        if (job.path !== null && chatId && job.path !== "/c/" + chatId) {
          failCapture(job);
          return;
        }
        try {
          const frame = JSON.stringify({
            result: {
              response: {
                ...(chatId ? { conversationId: chatId } : {}),
                modelResponse: { message, partial: false },
              },
            },
          });
          const bytes = new TextEncoder().encode(frame + "\n");
          if (bytes.length > LIMIT) throw new Error("Completion response exceeded the size limit");
          const requestTurn = {
            chat_id: chatId,
            prompt: job.prompt,
            endpoint: chatId
              ? "/rest/app-chat/conversations/" + chatId + "/responses"
              : "/rest/app-chat/conversations/new",
          };
          emitBody(job, 200, bytes, chatId ? "/c/" + chatId : null, requestTurn);
        } catch (_) {
          failCapture(job);
        }
      };
      const onMessage = event => {
        if (active?.nonce !== job.nonce) {
          socket.removeEventListener("message", onMessage);
          return;
        }
        let data;
        try { data = JSON.parse(typeof event.data === "string" ? event.data : wsDecode(event.data)); }
        catch (_) { return; }
        if (!data || typeof data !== "object") return;
        const ev = data.event && typeof data.event === "object" ? data.event : data;
        const type = typeof ev.type === "string" ? ev.type : "";
        if (type === "conversation.attached") {
          const id = ev.conversation && ev.conversation.id;
          if (typeof id === "string" && id) state.chatId = id;
          return;
        }
        if (type === "response.chunk") {
          const chunk = (ev.chunk && ev.chunk.text) || {};
          const text = typeof chunk.text === "string" ? chunk.text : "";
          const channel = typeof chunk.channel === "string" ? chunk.channel : "";
          if (!text || channel === "CHANNEL_ASSISTANT_THINKING") return;
          if (channel && channel !== "CHANNEL_ASSISTANT_RESPONSE") return;
          state.size += text.length;
          if (state.size > LIMIT) { finish(false); return; }
          state.texts.push(text);
          return;
        }
        if (type === "response.output_text.delta") {
          const delta = typeof ev.delta === "string" ? ev.delta : "";
          if (!delta || (ev.x_grok && ev.x_grok.is_thinking)) return;
          state.size += delta.length;
          if (state.size > LIMIT) { finish(false); return; }
          state.texts.push(delta);
          return;
        }
        if (type === "error" || (type === "response.grok.output" && ev.output && ev.output.stream_error)) {
          finish(false);
          return;
        }
        if (type === "response.done" || type === "response.completed") {
          const message = state.texts.join("");
          if (!message.trim()) { finish(false); return; }
          finish(true, message);
        }
      };
      socket.addEventListener("message", onMessage);
    };
    WebSocket.prototype.send = function(data) {
      try {
        const job = active;
        if (job && !job.observed) {
          const raw = wsDecode(data);
          if (page.socket === "grok" && raw.includes("response.create")) {
            let msg = null;
            try { msg = JSON.parse(raw); } catch (_) { /* ignore */ }
            const type = msg && (msg.event && msg.event.type || msg.type);
            if (type === "response.create" && beginCapture(job)) watchGrokSocket(this, job);
          } else if (page.socket === "mistral" && raw) {
            const tip = job.prompt.slice(0, Math.min(96, job.prompt.length));
            if ((tip && raw.includes(tip)) || raw.includes("messageInput")
                || raw.includes("generationStatus") || raw.includes("contentChunks")) {
              if (beginCapture(job)) watchMistralSocket(this, job);
            }
          }
        }
      } catch (_) { /* never break the site socket */ }
      return nativeWsSend.apply(this, arguments);
    };
  }

  // Frontends can cache fetch during startup; that reference must keep
  // observing future jobs even when the site later adds its own wrapper.
  const earlyObserver = createObserver(window.fetch.bind(window));
  window.fetch = earlyObserver;
})();
