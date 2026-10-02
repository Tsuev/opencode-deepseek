"""Opt-in real browser fixtures; all origins are intercepted, no live accounts."""

import base64
import json
import os
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from providers.browser_protocol import glm_answer, grok_answer

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.getenv("RUN_BROWSER_FIXTURES") == "1", "Opt-in isolated browser fixtures")
class ObserverBoundaryFixtureTests(unittest.TestCase):
    def observe(self, host, setup, exercise, wait):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body="<html></html>"))
                page = context.new_page()
                page.goto("https://" + host + "/")
                page.evaluate("""() => {
                    window.receipts = [];
                    document.addEventListener('opencode-local-response-v1', event => receipts.push(JSON.parse(event.detail)));
                    window.startOwnedJob = nonce => document.dispatchEvent(new CustomEvent('opencode-local-job-v1',
                      {detail:JSON.stringify({nonce,prompt:'owned prompt',path:'/work/owned-chat'})}));
                }""")
                page.evaluate(setup)
                page.add_script_tag(content=(ROOT / "browser/opencode-observer.user.js").read_text())
                page.evaluate(exercise)
                page.wait_for_function(wait)
                return page.evaluate("receipts")
            finally:
                browser.close()

    def test_mistral_only_exact_structured_prompt_exports_its_request_turn(self):
        receipts = self.observe("chat.mistral.ai", """() => {
            window.fetch = async () => new Response('owned response', {headers:{'Content-Type':'text/event-stream'}});
        }""", """async () => {
            startOwnedJob('current-job');
            for (const [messageId,text] of [['neighbor','prefix owned prompt'],['owned-user','owned prompt']]) {
                await fetch('/api/reply', {method:'POST',body:JSON.stringify({chatId:'owned-chat',messageId,
                    messageInput:[{type:'text',text}],credentials:'must never be exported'})});
            }
        }""", "receipts.some(r => r.body)")
        results = [r for r in receipts if "body" in r]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["request_turn"], {"chat_id":"owned-chat","user_id":"owned-user","version":0})
        self.assertNotIn("credentials", json.dumps(receipts))

    def test_late_previous_job_cannot_export_neighbor_request_evidence(self):
        receipts = self.observe("chat.mistral.ai", """() => {
            window.replies = [];
            window.fetch = () => new Promise(resolve => replies.push(resolve));
        }""", """async () => {
            const send = messageId => fetch('/api/reply', {method:'POST',body:JSON.stringify({
                chatId:'owned-chat',messageId,messageInput:[{type:'text',text:'owned prompt'}]})});
            startOwnedJob('old-job'); void send('old-user');
            while (replies.length < 1) await new Promise(resolve => setTimeout(resolve,0));
            startOwnedJob('new-job'); void send('new-user');
            while (replies.length < 2) await new Promise(resolve => setTimeout(resolve,0));
            replies[0](new Response('old response',{headers:{'Content-Type':'text/event-stream'}}));
            replies[1](new Response('new response',{headers:{'Content-Type':'text/event-stream'}}));
        }""", "receipts.some(r => r.body)")
        results = [r for r in receipts if "body" in r]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["nonce"], "new-job")
        self.assertEqual(results[0]["request_turn"]["user_id"], "new-user")
        self.assertEqual(base64.b64decode(results[0]["body"]), b"new response")

    def test_mistral_new_chat_capture_does_not_invent_request_turn_evidence(self):
        receipts = self.observe("chat.mistral.ai", """() => {
            window.fetch = async () => new Response('owned bootstrap stream',{headers:{'Content-Type':'text/event-stream'}});
        }""", """async () => {
            document.dispatchEvent(new CustomEvent('opencode-local-job-v1',
                {detail:JSON.stringify({nonce:'new-chat',prompt:'owned prompt',path:null})}));
            await fetch('/api/new-chat',{method:'POST',body:JSON.stringify({content:[{type:'text',text:'owned prompt'}]})});
        }""", "receipts.some(r => r.body)")
        result = next(r for r in receipts if "body" in r)
        self.assertNotIn('request_turn', result)

    def connect_receipts(self, trailing=False):
        return self.observe("www.kimi.ai", """() => {
            const envelope = (flags,text) => {const bytes=new TextEncoder().encode(text), result=new Uint8Array(5+bytes.length);
                result[0]=flags;new DataView(result.buffer).setUint32(1,bytes.length);result.set(bytes,5);return result;};
            const first=envelope(0,'{"heartbeat":{}}'), end=envelope(2,'{}');
            window.fetch=async () => new Response(new ReadableStream({start(controller){
                controller.enqueue(first.slice(0,3));controller.enqueue(first.slice(3));
                controller.enqueue(end.slice(0,4));
                const last=new Uint8Array(end.length-4+TRAILING);last.set(end.slice(4));controller.enqueue(last);
                // Intentionally leave the frontend's stream open after end.
            }}));
        }""".replace("TRAILING", "1" if trailing else "0"), """async () => {
            startOwnedJob('owned-connect');
            await fetch('/apiv2/kimi.gateway.chat.v1.ChatService/Chat',{method:'POST',body:'owned prompt'});
        }""", "receipts.some(r => r.body || r.error)")

    def test_kimi_end_envelope_finishes_capture_without_waiting_for_wrapper_eof(self):
        receipts = self.connect_receipts()
        result = next(r for r in receipts if "body" in r)
        from providers.browser_protocol import connect_completed
        connect_completed(base64.b64decode(result["body"]))

    def test_kimi_bytes_after_end_envelope_are_rejected(self):
        receipts = self.connect_receipts(trailing=True)
        self.assertFalse(any("body" in r for r in receipts))
        self.assertTrue(any("error" in r for r in receipts))


@unittest.skipUnless(os.getenv("RUN_BROWSER_FIXTURES") == "1", "Opt-in isolated browser fixtures")
class UserscriptFixtureTests(unittest.TestCase):
    def fixture(self, draft="", unrelated=False, idle_recovery=False, observer=True, navigation=False, completion_path="/api/chat/completions", delayed_editor=False, request_object=False, cancel_before_editor=False, abandon_first=False, grok_editor=False, recover_cancelled=False, user_edit_on_cancel=False, replaced_observer=False, async_insertion=False, cached_fetch=False, two_jobs=False, edit_newlines=False, expire_before_insertion=False):
        prompt = "User:\nReply ONLY with FIXTURE_OK"
        job = {"id":"fixture-job", "provider":"glm", "prompt":prompt,
               "path":"/c/fixture", "lease":"fixture-lease", "submitted":False, "expires_in":180}
        if navigation:
            job["path"] = None
        if expire_before_insertion:
            job['expires_in'] = .05
        body = ('data: ' + json.dumps({"type":"chat:completion","data":{"phase":"answer","content":"FIXTURE_OK","done":True}}) + '\n\n').encode()
        if grok_editor:
            job["provider"] = "grok"
            completion_path = "/rest/app-chat/conversations/new"
            body = json.dumps({"result":{"response":{"modelResponse":{"message":"FIXTURE_OK","partial":False}}}}).encode()
        results, upstream = [], []
        store = {}
        polls = 0
        abandoned = False
        cancelled = False
        navigation_requests = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context()

                def route(request_route):
                    request = request_route.request
                    if navigation and urlsplit(request.url).path == "/":
                        # Hold navigation open while the old document is alive.
                        navigation_requests.append(request_route)
                        return
                    if request.method == "POST":
                        upstream.append(request.post_data)
                        if urlsplit(request.url).path == "/api/chats/new":
                            request_route.fulfill(status=200,content_type="application/json",body=b'{"id":"created"}')
                        else:
                            request_route.fulfill(status=200,content_type="text/event-stream",body=body)
                    else:
                        # An owned fixture with a real textarea and send handler.
                        html = '''<textarea id="chat-input"></textarea><script>
                          const editor = document.getElementById('chat-input');
                          editor.addEventListener('keydown', async event => {
                            if (event.key !== 'Enter') return;
                            /* unrelated-request */
                            const response = await fetch('/api/chat/completions', {
                              method:'POST', headers:{'Content-Type':'application/json'},
                              body:JSON.stringify({message:editor.value})
                            });
                            window.frontendReceivedResponse = true;
                            await response.text();
                          });
                        </script>'''
                        if unrelated:
                            html = html.replace('/* unrelated-request */', "await fetch('/api/chats/new', {method:'POST', body:JSON.stringify({message:editor.value})});")
                        html = html.replace("/api/chat/completions", completion_path)
                        if grok_editor:
                            markup = '<form data-composer="true"><textarea id="chat-input" aria-label="Ask Grok anything"></textarea></form>' if grok_editor == 'textarea' else '<form data-composer="true"><textarea aria-hidden="true" style="visibility:hidden;position:absolute"></textarea><div id="chat-input" class="query-bar-editor" contenteditable="true" aria-label="Задай Grok любой вопрос"></div></form>'
                            html = html.replace('<textarea id="chat-input"></textarea>', markup)
                            html = html.replace("editor.value", "window.committedDraft")
                            read = 'editor.value' if grok_editor == 'textarea' else 'editor.innerText'
                            html = html.replace("editor.addEventListener('keydown'", "editor.addEventListener('input', () => setTimeout(() => window.committedDraft = " + read + ", 0)); editor.closest('form').addEventListener('submit'")
                            html = html.replace("if (event.key !== 'Enter') return;", "event.preventDefault();")
                        if cached_fetch:
                            html = html.replace('const editor =', 'window.cachedSiteFetch = window.fetch; const editor =')
                            html = html.replace('await fetch(', 'await window.cachedSiteFetch(')
                        if two_jobs:
                            html = html.replace('await response.text();', "await response.text(); editor.value = ''; editor.textContent = '';")
                        if abandon_first:
                            html = html.replace('/* unrelated-request */', "if (!window.fixtureSent) {window.fixtureSent=1;editor.value='';return;}")
                        if request_object:
                            html = html.replace("fetch('/api/chat/completions', {", "fetch(new Request('/api/chat/completions', {")
                            html = html.replace("body:JSON.stringify({message:editor.value})\n                            });", "body:JSON.stringify({message:editor.value})\n                            }));")
                        if delayed_editor:
                            html = html.replace('<textarea id="chat-input">', '<textarea id="chat-input" style="display:none">')
                            html = html.replace('const editor =', 'setTimeout(() => document.querySelector("textarea").style.display = "", 1500); const editor =')
                        request_route.fulfill(status=200,content_type="text/html",body=html)

                context.route("**/*", route)

                def rpc(_, value):
                    nonlocal polls, abandoned, cancelled
                    path = urlsplit(value["url"]).path
                    if path.startswith("/browser/jobs/"):
                        polls += 1
                        if idle_recovery and polls == 1:
                            return {"status":503,"responseText":'{}'}
                        if idle_recovery:
                            return {"status":200,"responseText":'{"job":null}'}
                        current = {**job, "id":"fixture-job-2", "prompt":prompt + '\nSECOND_REQUEST'} if abandoned or cancelled or (two_jobs and results) else job
                        payload = {"job":None if len(results) >= (2 if recover_cancelled or two_jobs else 1) else current}
                    elif path == "/browser/check":
                        if recover_cancelled and not cancelled and value.get('fixtureInserted'):
                            cancelled = True
                            payload = {"active":False}
                        elif abandon_first and not abandoned and value.get('fixtureSent'):
                            abandoned = True
                            payload = {"active":False}
                        else:
                            payload = {"active":not (cancel_before_editor and (not grok_editor or value.get('fixtureInserted')))}
                    elif path in ("/browser/submit", "/browser/navigate"):
                        payload = {"ok":True}
                    elif path == "/browser/result":
                        results.append(json.loads(value["data"]))
                        if (recover_cancelled or edit_newlines) and len(results) == 1:
                            return {"status":409,"responseText":'{}'}
                        payload = {"ok":True}
                    else:
                        raise AssertionError("Unexpected local request")
                    self.assertEqual(value["headers"]["Authorization"], "Bearer " + "x" * 43)
                    return {"status":200,"responseText":json.dumps(payload)}

                context.expose_binding("fixtureRpc", rpc)
                context.expose_binding("fixtureGet", lambda _, key, default:store.get(key,default))
                context.expose_binding("fixtureSet", lambda _, key, value:store.update({key:value}))
                context.add_init_script('''
                    window.fixtureRoots = [];
                    window.fixtureEventTypes = [];
                    for (const channel of ['opencode-local-job-v1','opencode-local-ready-v1','opencode-local-response-v1']) {
                      document.addEventListener(channel, event => window.fixtureEventTypes.push(typeof event.detail));
                    }
                    const nativeShadow = HTMLElement.prototype.attachShadow;
                    HTMLElement.prototype.attachShadow = function(options) {
                      const root = nativeShadow.call(this, options);
                      window.fixtureRoots.push(root); return root;
                    };
                    window.fixtureResponseClones = 0;
                    const nativeClone = Response.prototype.clone;
                    Response.prototype.clone = function() { window.fixtureResponseClones++; return nativeClone.call(this); };
                    window.GM = {xmlHttpRequest:async value => {
                      window.fixtureRpcCalls = (window.fixtureRpcCalls || 0) + 1;
                      const editor = document.querySelector('#chat-input');
                      const response = await window.fixtureRpc({...value,
                        fixtureInserted:!!(editor?.value || editor?.textContent), fixtureSent:!!window.fixtureSent});
                      if (window.fixtureExpire && value.url.endsWith('/browser/submit')) await new Promise(resolve => setTimeout(resolve,150));
                      if (window.fixtureUserEdit && value.url.endsWith('/browser/check') && response.responseText === '{"active": false}') {
                        const editor = document.querySelector('#chat-input');
                        editor.textContent = 'user edited owned fixture';
                        editor.dispatchEvent(new Event('input', {bubbles:true}));
                      }
                      return response;
                    },getValue:window.fixtureGet,setValue:window.fixtureSet};
                ''')
                if observer:
                    context.add_init_script((ROOT/"browser/opencode-observer.user.js").read_text())
                page = context.new_page()
                page.goto("https://grok.com/c/fixture" if grok_editor else "https://chat.z.ai/c/fixture")
                if draft:
                    page.locator("#chat-input").fill(draft)
                if user_edit_on_cancel:
                    page.evaluate("window.fixtureUserEdit = true")
                if expire_before_insertion:
                    page.evaluate("window.fixtureExpire = true")
                if replaced_observer:
                    page.evaluate("const siteFetch = window.fetch; window.siteWrapper = (...args) => { window.siteFetchUsed = true; return siteFetch(...args); }; window.fetch = window.siteWrapper;")
                if async_insertion:
                    page.evaluate("const insert = document.execCommand.bind(document); document.execCommand = (command, ...args) => { if(command === 'insertText'){setTimeout(() => insert(command, ...args),0);return true;}return insert(command,...args);}")
                if edit_newlines:
                    page.evaluate("document.querySelector('#chat-input').addEventListener('input', () => { if(window.fixtureEdited) return; window.fixtureEdited = true; setTimeout(() => { const editor = document.querySelector('#chat-input'); const text = (editor.isContentEditable ? editor.innerText : editor.value).replaceAll('\\n',''); if(editor.isContentEditable) editor.textContent = text; else editor.value = text; },0); })")
                controller = (ROOT/"browser/opencode-controller.user.js").read_text().replace("__BRIDGE_TOKEN__","x"*43,1)
                page.add_script_tag(content=controller)
                page.wait_for_function("window.fixtureRoots.length > 0 && window.fixtureRoots[0].querySelector('button')")
                page.evaluate("window.fixtureRoots[0].querySelector('button').click()")
                # A binding resolves only once the complete result has arrived.
                page.wait_for_function("window.fixtureRoots[0].host.style.position === 'fixed'")
                if navigation:
                    page.wait_for_timeout(1700)
                    self.assertEqual(len(navigation_requests),1)
                    self.assertEqual(polls,1)
                    self.assertEqual(results,[])
                    return 'navigating', upstream, '', prompt
                if idle_recovery:
                    page.wait_for_function("window.fixtureRpcCalls >= 2 && window.fixtureRoots[0].querySelector('button').textContent === 'OpenCode: подключено · отключить'")
                    self.assertGreaterEqual(polls,2)
                    self.assertEqual(results,[])
                    return 'connected', upstream, page.locator("#chat-input").input_value(), prompt
                deadline = __import__("time").monotonic() + 5
                while not results and __import__("time").monotonic() < deadline:
                    page.wait_for_timeout(50)
                self.assertEqual(len(results),1)
                if replaced_observer:
                    self.assertTrue(page.evaluate('window.siteFetchUsed === true'))
                    self.assertTrue(page.evaluate('window.fetch === window.siteWrapper'))
                    self.assertEqual(page.evaluate('window.fixtureResponseClones'),1)
                if edit_newlines:
                    page.wait_for_function("window.fixtureRoots[0].querySelector('button').textContent === 'OpenCode: остановлено · подключить'")
                    value = page.locator('#chat-input').evaluate('e => e.isContentEditable ? e.textContent : e.value')
                    self.assertEqual(value,prompt.replace('\n',''))
                    return results[0], upstream, value, prompt
                if two_jobs:
                    deadline = __import__('time').monotonic() + 5
                    while len(results) < 2 and __import__('time').monotonic() < deadline:
                        page.wait_for_timeout(50)
                    self.assertEqual(len(results),2)
                    self.assertEqual(page.evaluate('window.fixtureResponseClones'),2)
                if recover_cancelled:
                    page.wait_for_function("window.fixtureRoots[0].querySelector('button').textContent === 'OpenCode: остановлено · подключить'")
                    value = page.locator('#chat-input').text_content()
                    if user_edit_on_cancel:
                        self.assertEqual(value,'user edited owned fixture')
                        return results[0], upstream, value, prompt
                    self.assertEqual(value,'')
                    page.evaluate("window.fixtureRoots[0].querySelector('button').click()")
                    page.wait_for_function("window.frontendReceivedResponse === true")
                    deadline = __import__('time').monotonic() + 5
                    while len(results) < 2 and __import__('time').monotonic() < deadline:
                        page.wait_for_timeout(50)
                    self.assertEqual(len(results),2)
                self.assertTrue(page.evaluate("window.fixtureEventTypes.every(value => value === 'string')"))
                value = page.locator("#chat-input").evaluate('e => e.isContentEditable ? e.textContent : e.value')
                return results[-1], upstream, value, prompt
            finally:
                for held_route in navigation_requests:
                    try:
                        held_route.abort()
                    except Exception:
                        pass  # A replacement navigation may already have cancelled it.
                context.unroute_all(behavior="ignoreErrors")
                browser.close()

    def test_site_send_handler_and_response_capture_finish_one_job(self):
        result, requests, _, prompt = self.fixture()
        self.assertEqual(len(requests),1)
        self.assertEqual(json.loads(requests[0])["message"],prompt)
        self.assertEqual(result["result"]["path"],"/c/fixture")
        self.assertEqual(glm_answer(base64.b64decode(result["result"]["body"])),"FIXTURE_OK")

    def test_existing_draft_is_preserved_and_no_prompt_is_sent(self):
        draft = "unsent fixture draft"
        result, requests, value, _ = self.fixture(draft)
        self.assertEqual(requests,[])
        self.assertEqual(value,draft)
        self.assertIn("error",result["result"])

    def test_noncompletion_post_with_the_prompt_cannot_finish_the_job(self):
        result, requests, _, _ = self.fixture(unrelated=True)
        self.assertEqual(len(requests),2)
        self.assertEqual(glm_answer(base64.b64decode(result["result"]["body"])),"FIXTURE_OK")

    def test_idle_poll_restores_connected_status_after_temporary_local_failure(self):
        status, requests, _, _ = self.fixture(idle_recovery=True)
        self.assertEqual(status,'connected')
        self.assertEqual(requests,[])

    def test_missing_observer_fails_before_the_prompt_is_sent(self):
        result, requests, _, _ = self.fixture(observer=False)
        self.assertEqual(requests,[])
        self.assertIn('error',result['result'])

    def test_site_fetch_wrapper_is_preserved_and_job_observer_is_installed_after_it(self):
        result, requests, _, _ = self.fixture(replaced_observer=True)
        self.assertEqual(len(requests),1)
        self.assertEqual(glm_answer(base64.b64decode(result['result']['body'])),'FIXTURE_OK')

    def test_async_native_insertion_is_visible_before_site_submission(self):
        result, requests, _, prompt = self.fixture(grok_editor=True,async_insertion=True)
        self.assertEqual(len(requests),1)
        self.assertEqual(json.loads(requests[0])['message'],prompt)
        self.assertEqual(grok_answer(base64.b64decode(result['result']['body'])),'FIXTURE_OK')

    def test_cached_startup_fetch_observes_two_jobs_without_duplicate_capture(self):
        result, requests, _, prompt = self.fixture(cached_fetch=True,two_jobs=True)
        self.assertEqual(result['id'],'fixture-job-2')
        self.assertEqual(len(requests),2)
        self.assertEqual(json.loads(requests[0])['message'],prompt)
        self.assertEqual(json.loads(requests[1])['message'],prompt + '\nSECOND_REQUEST')
        self.assertEqual(glm_answer(base64.b64decode(result['result']['body'])),'FIXTURE_OK')

    def test_user_removing_prompt_newline_is_preserved_and_cannot_send(self):
        for editor in (True,'textarea'):
            with self.subTest(editor=editor):
                result, requests, value, prompt = self.fixture(grok_editor=editor,edit_newlines=True)
                self.assertEqual(requests,[])
                self.assertEqual(value,prompt.replace('\n',''))
                self.assertIn('error',result['result'])

    def test_slow_navigation_is_not_restarted_by_polling(self):
        status, requests, _, _ = self.fixture(navigation=True)
        self.assertEqual(status,'navigating')
        self.assertEqual(requests,[])

    def test_glm_v2_completion_route_is_observed(self):
        result, requests, _, _ = self.fixture(completion_path="/api/v2/chat/completions")
        self.assertEqual(len(requests),1)
        self.assertEqual(glm_answer(base64.b64decode(result["result"]["body"])),"FIXTURE_OK")

    def test_delayed_editor_is_ready_before_the_prompt_is_sent(self):
        result, requests, _, _ = self.fixture(delayed_editor=True)
        self.assertEqual(len(requests),1)
        self.assertEqual(glm_answer(base64.b64decode(result["result"]["body"])),"FIXTURE_OK")

    def test_fetch_request_keeps_its_original_body_and_is_sent_once(self):
        result, requests, _, prompt = self.fixture(request_object=True)
        self.assertEqual(len(requests),1)
        self.assertEqual(json.loads(requests[0])["message"],prompt)
        self.assertEqual(glm_answer(base64.b64decode(result["result"]["body"])),"FIXTURE_OK")

    def test_cancelled_job_does_not_send_after_editor_hydration(self):
        result, requests, _, _ = self.fixture(delayed_editor=True, cancel_before_editor=True)
        self.assertEqual(requests,[])
        self.assertIn('error',result['result'])

    def test_glm_expired_authorization_does_not_insert_or_send(self):
        result, requests, value, _ = self.fixture(expire_before_insertion=True)
        self.assertEqual(requests, [])
        self.assertEqual(value, '')
        self.assertIn('error', result['result'])

    def test_retired_job_without_response_releases_controller_for_next_job(self):
        result, requests, _, prompt = self.fixture(abandon_first=True)
        self.assertEqual(result['id'],'fixture-job-2')
        self.assertEqual(len(requests),1)
        self.assertEqual(json.loads(requests[0])['message'],prompt + '\nSECOND_REQUEST')

    def test_grok_contenteditable_uses_site_handler_and_observes_one_response(self):
        result, requests, _, prompt = self.fixture(grok_editor=True)
        self.assertEqual(len(requests),1)
        self.assertEqual(json.loads(requests[0])["message"],prompt)
        self.assertEqual(grok_answer(base64.b64decode(result["result"]["body"])),"FIXTURE_OK")

    def test_grok_contenteditable_preserves_an_unsent_draft(self):
        result, requests, value, _ = self.fixture(grok_editor=True, draft="owned draft")
        self.assertEqual(requests,[])
        self.assertEqual(value,"owned draft")
        self.assertIn("error",result["result"])

    def test_cancelled_grok_job_cannot_submit_its_form_after_insertion(self):
        result, requests, value, _ = self.fixture(grok_editor=True, cancel_before_editor=True)
        self.assertEqual(requests,[])
        self.assertEqual(value,'')
        self.assertIn("error",result["result"])

    def test_cancelled_insertion_is_cleaned_despite_409_and_next_job_can_send(self):
        result, requests, _, prompt = self.fixture(grok_editor=True, recover_cancelled=True)
        self.assertEqual(result['id'],'fixture-job-2')
        self.assertEqual(len(requests),1)
        self.assertEqual(json.loads(requests[0])['message'],prompt + '\nSECOND_REQUEST')
        self.assertEqual(grok_answer(base64.b64decode(result['result']['body'])),'FIXTURE_OK')

    def test_cancelled_insertion_preserves_user_edit_despite_409(self):
        result, requests, value, _ = self.fixture(grok_editor=True, recover_cancelled=True, user_edit_on_cancel=True)
        self.assertEqual(requests,[])
        self.assertEqual(value,'user edited owned fixture')
        self.assertIn('error',result['result'])
