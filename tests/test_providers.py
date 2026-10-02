import asyncio
import base64
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from providers.antigravity import AntigravityClient, parse_result, parse_stream
from providers.browser_protocol import connect_completed, glm_answer, grok_answer, mistral_answer
from providers.common import ProviderUnavailable, completion_timeout
from providers.conversations import decode, encode
from providers.tab_bridge import parse_browser_result

UUID = "12345678-1234-4234-8234-123456789abc"


def envelope(flags, payload):
    return bytes([flags]) + struct.pack(">I", len(payload)) + payload


def sse(obj):
    return ("data: " + json.dumps(obj) + "\n\n").encode()


def cli_init(model="flash"):
    return {"event":"init", "init":{"tools":[], "agent":"opencode-text-bridge",
            "model":model, "permission_mode":"request-review"}}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        from tests.support import isolated_access
        isolated_access(self)

    def test_model_cannot_be_a_cli_flag_before_process_launch(self):
        with patch("providers.antigravity.cli_path") as binary:
            for model in ("--dangerously-skip-permissions", "flash\n--agent", "flash pro"):
                with self.assertRaises(ValueError):
                    AntigravityClient().chat("marker",model=model)
            binary.assert_not_called()

    def test_conversation_tokens_are_scoped_and_paths_cannot_escape(self):
        for provider, reference in (("gemini", UUID), ("grok", "/c/" + UUID),
                                    ("glm", "/c/" + UUID), ("mistral", "/chat/" + UUID),
                                    ("kimi", "/chat/" + UUID), ("mistral", "/work/" + UUID)):
            token = encode(provider, "default", reference)
            self.assertEqual(decode(token, provider), ("default", reference))
            for other in {"gemini", "grok", "glm", "mistral", "kimi"} - {provider}:
                with self.assertRaises(ValueError):
                    decode(token, other)
        for path in ("https://evil.test/", "//evil.test/", "/c/../auth", "/c/a?redirect=evil", "/c/a%2fb"):
            with self.assertRaises(ValueError):
                encode("grok", "default", path)

    def test_malformed_tokens_cannot_supply_a_reference(self):
        for value in ("web:grok:!", "web:grok:" + "a" * 1025,
                      "web:grok:" + base64.urlsafe_b64encode(b'{"url":"https://evil"}').decode()):
            with self.assertRaises(ValueError):
                decode(value, "grok")

    def test_grok_partial_or_error_tail_cannot_succeed(self):
        final = {"result": {"response": {"modelResponse": {"message": "answer", "partial": False}}}}
        body = json.dumps(final).encode()
        self.assertEqual(grok_answer(body), "answer")
        for bad in (json.dumps({"result": {"response": {"token": "partial"}}}).encode(),
                    body + b'\n{"error":"quota"}', body + b"\n" + body,
                    body.replace(b'false', b'true')):
            with self.assertRaises(ProviderUnavailable):
                grok_answer(bad)

    def test_grok_resumed_shape_and_nested_errors(self):
        model = {"message":"answer", "partial":False}
        for wrapper in (lambda value: {"result":{"response":{"modelResponse":value}}},
                        lambda value: {"result":{"modelResponse":value}}):
            self.assertEqual(grok_answer(json.dumps(wrapper(model)).encode()), "answer")
            for error in ({"error":True}, {"streamErrors":[{"error":"fatal"}]}):
                with self.assertRaises(ProviderUnavailable):
                    grok_answer(json.dumps(wrapper({**model, **error})).encode())

    def test_glm_requires_completion_and_excludes_reasoning(self):
        body = (sse({"type":"chat:completion","data":{"phase":"thinking","delta_content":"secret reasoning"}})
                + sse({"type":"chat:completion","data":{"phase":"answer","delta_content":"answer"}}))
        with self.assertRaises(ProviderUnavailable):
            glm_answer(body + b"data: [DONE]\n\n")
        finished = body + sse({"type":"chat:completion","data":{"done":True}})
        self.assertEqual(glm_answer(finished), "answer")
        with self.assertRaises(ProviderUnavailable):
            glm_answer(finished + sse({"error":"failed"}))

    def test_connect_needs_a_successful_end_envelope(self):
        message = envelope(0, b"\x0a\x06answer")
        end = envelope(2, b'{"metadata":{}}')
        connect_completed(message + end)
        for bad in (message, message + end[:-1], message + envelope(2, b'{"error":{"code":"unauthenticated"}}'),
                    message + end + message, envelope(1, b"compressed") + end, end):
            with self.assertRaises(ProviderUnavailable):
                connect_completed(bad)

    def test_glm_reasoning_completion_cannot_finalize_an_answer(self):
        answer = sse({"type":"chat:completion","data":{"phase":"answer","content":"partial"}})
        reasoning_end = sse({"type":"chat:completion","data":{"phase":"thinking","done":True}})
        complete = sse({"type":"chat:completion","data":{"phase":"answer","done":True}})
        for body in (answer + reasoning_end, reasoning_end + answer, answer + complete + answer):
            with self.assertRaises(ProviderUnavailable):
                glm_answer(body)

    def test_glm_v2_global_terminal_confirms_only_the_main_answer(self):
        answer = sse({"type":"chat:completion","data":{"phase":"answer","delta_content":"answer"}})
        terminal = sse({"type":"chat:completion","data":{"phase":"done","done":True}})
        subagent = sse({"type":"chat:completion","data":{"scope":"subagent","phase":"done","done":True}})
        self.assertEqual(glm_answer(answer + terminal), "answer")
        for body in (terminal, answer + subagent, answer + terminal + answer):
            with self.assertRaises(ProviderUnavailable):
                glm_answer(body)

    def test_mistral_missing_terminal_and_errors_are_rejected(self):
        body = sse({"choices":[{"delta":{"content":"answer"},"finish_reason":None}]})
        with self.assertRaises(ProviderUnavailable):
            mistral_answer(body)
        finished = body + sse({"choices":[{"delta":{},"finish_reason":"stop"}]})
        self.assertEqual(mistral_answer(finished), "answer")
        with self.assertRaises(ProviderUnavailable):
            mistral_answer(finished + sse({"error":"quota"}))

    def test_mistral_never_appends_text_after_terminal_completion(self):
        text = sse({"choices":[{"delta":{"content":"answer"},"finish_reason":None}]})
        stop = sse({"choices":[{"delta":{},"finish_reason":"stop"}]})
        delta = sse({"type":"message.delta","text":"late"})
        complete = sse({"type":"message.completed"})
        self.assertEqual(mistral_answer(text + stop + b'data: [DONE]\n\n'), "answer")
        for body in (text + stop + text, text + complete + delta,
                     text + complete + complete, text + b'data: [DONE]\n\n' + delta):
            with self.assertRaises(ProviderUnavailable):
                mistral_answer(body)

    def mistral_patch_fixture(self):
        def record(value):
            return ("15:" + json.dumps({"json":value}) + "\n").encode()
        bootstrap = record({"type":"bootstrap", "chat":{"id":"owned-chat"},
                            "messages":[{"id":"owned-user","role":"user","version":0,"content":"owned prompt"}]})
        def message(patches, message_id="owned-assistant", version=0):
            return record({"type":"message","messageId":message_id,"messageVersion":version,"patches":patches})
        root = message([{"op":"replace","path":"/","value":{"role":"assistant","id":"owned-assistant",
            "version":0,"parentId":"owned-user","parentVersion":0,"chatId":"owned-chat",
            "content":"","contentChunks":None,"generationStatus":"in-progress"}}])
        chunks = message([{"op":"replace","path":"/contentChunks","value":[{"type":"text","text":"ans"}]}])
        append = message([{"op":"append","path":"/contentChunks/0/text","value":"wer"}])
        success = message([{"op":"replace","path":"/generationStatus","value":"success"}])
        title = record({"type":"chat","patches":[{"op":"replace","path":"/generatedTitle","value":"owned title"}]})
        return bootstrap, root, chunks, append, success, title, message

    def test_mistral_current_patch_stream_binds_completed_assistant(self):
        bootstrap, root, chunks, append, success, title, message = self.mistral_patch_fixture()
        moderation = message([{"op":"replace","path":"/moderationCategory","value":"safe"}],"owned-user")
        body = bootstrap + root + moderation + chunks + append + title + success + b'8:null\n' + title
        self.assertEqual(mistral_answer(body,"owned prompt"),'answer')
        with self.assertRaises(ProviderUnavailable):
            mistral_answer(body,"different prompt")

    def test_mistral_patch_stream_rejects_partial_late_and_wrong_turns(self):
        bootstrap, root, chunks, append, success, title, message = self.mistral_patch_fixture()
        prefix = bootstrap + root + chunks + append
        failure = message([{"op":"replace","path":"/generationStatus","value":"failed"}])
        wrong_version = message([{"op":"append","path":"/contentChunks/0/text","value":"late"}],version=1)
        for body in (prefix, prefix + b'8:null\n', prefix + success,
                     prefix + failure + b'8:null\n', prefix + wrong_version + success + b'8:null\n',
                     prefix + success + append + b'8:null\n', prefix + success + b'8:null\n8:null\n',
                     bootstrap + root.replace(b'owned-user',b'other-user') + chunks + success + b'8:null\n',
                     prefix + success + b'8:null\n' + append):
            with self.subTest(body=body), self.assertRaises(ProviderUnavailable):
                mistral_answer(body,"owned prompt")

    def test_mistral_wire_chat_must_match_validated_result_and_resume_route(self):
        bootstrap, root, chunks, append, success, _, _ = self.mistral_patch_fixture()
        body = base64.b64encode(bootstrap + root + chunks + append + success + b'8:null\n').decode()
        for prefix in ('/work/','/chat/'):
            path = prefix + 'owned-chat'
            result = {'status':200,'path':path,'body':body,
                      'request_turn':{'user_id':'owned-user','version':0,'chat_id':'owned-chat'}}
            self.assertEqual(parse_browser_result('mistral',result,path,'owned prompt').text,'answer')
            other = prefix + 'other-chat'
            with self.assertRaises(ProviderUnavailable):
                parse_browser_result('mistral',{**result,'path':other},other,'owned prompt')

    def test_mistral_temporary_chunks_are_removed_before_final_answer(self):
        bootstrap, root, chunks, _, success, _, message = self.mistral_patch_fixture()
        temporary = message([{'op':'add','path':'/contentChunks/1','value':{'type':'text','text':'temporary tool-call text','_context':{'type':'reasoning'}}},
                             {'op':'add','path':'/contentChunks/2','value':{'type':'text','text':'temporary second chunk'}}])
        final = message([{'op':'replace','path':'/contentChunks/1/_context/endTime','value':123},
                         {'op':'remove','path':'/contentChunks/2'},
                         {'op':'remove','path':'/contentChunks/1/_context'},
                         {'op':'replace','path':'/contentChunks/1/text','value':''},
                         {'op':'append','path':'/contentChunks/0/text','value':'wer'}])
        prefix = bootstrap + root + chunks + temporary
        self.assertEqual(mistral_answer(prefix+final+success+b'8:null\n','owned prompt','owned-chat'),'answer')
        for patch in ({'op':'add','path':'/contentChunks/99','value':{'type':'text','text':'wrong'}},
                      {'op':'remove','path':'/contentChunks/99'},
                      {'op':'replace','path':'/contentChunks/0/text','value':{}},
                      {'op':'replace','path':'/contentChunks/0/unknown','value':'wrong'}):
            with self.subTest(patch=patch), self.assertRaises(ProviderUnavailable):
                mistral_answer(prefix+message([patch])+success+b'8:null\n','owned prompt','owned-chat')

    def test_mistral_reasoning_context_never_becomes_final_answer_text(self):
        bootstrap, root, _, _, success, _, message = self.mistral_patch_fixture()
        chunks = message([{'op':'replace','path':'/contentChunks','value':[
            {'type':'text','text':'owned reasoning','_context':{'type':'reasoning'}},
            {'type':'text','text':'answer'}]}])
        self.assertEqual(mistral_answer(bootstrap+root+chunks+success+b'8:null\n','owned prompt','owned-chat'),'answer')
        for context in (None,{'type':'unknown'}):
            bad = message([{'op':'replace','path':'/contentChunks','value':[{'type':'text','text':'answer','_context':context}]}])
            with self.assertRaises(ProviderUnavailable):
                mistral_answer(bootstrap+root+bad+success+b'8:null\n','owned prompt','owned-chat')

    def test_mistral_resume_requires_matched_request_turn(self):
        bootstrap, root, chunks, append, success, _, _ = self.mistral_patch_fixture()
        body = root + chunks + append + success + b'8:null\n'
        turn = {'user_id':'owned-user','version':0,'chat_id':'owned-chat'}
        result = {'status':200,'path':'/work/owned-chat','body':base64.b64encode(body).decode(),'request_turn':turn}
        self.assertEqual(parse_browser_result('mistral',result,result['path'],'owned prompt').text,'answer')
        for invalid in (None, {**turn,'user_id':'other-user'}, {**turn,'version':1},
                        {**turn,'chat_id':'other-chat'}, {**turn,'chat_id':None}):
            with self.subTest(turn=invalid), self.assertRaises(ProviderUnavailable):
                parse_browser_result('mistral',{**result,'request_turn':invalid},result['path'],'owned prompt')
        with self.assertRaises(ProviderUnavailable):
            mistral_answer(bootstrap+body,'owned prompt','owned-chat',{**turn,'user_id':'other-user'})
        with self.assertRaises(ProviderUnavailable):
            parse_browser_result('mistral',{**result,'request_turn':None},None,'owned prompt')

    def test_mistral_browser_api_rejects_unbound_legacy_streams(self):
        legacy = sse({'type':'message.delta','text':'unbound legacy answer'}) + sse({'type':'message.completed'}) + b'data: [DONE]\n\n'
        result = {'status':200,'path':'/work/owned-chat','body':base64.b64encode(legacy).decode()}
        for path, turn in ((None,None), ('/work/owned-chat',{'chat_id':'owned-chat'}),
                           ('/work/owned-chat',{'chat_id':'owned-chat','user_id':'owned-user','version':99}),
                           ('/work/owned-chat',{'chat_id':'owned-chat','user_id':'owned-user','version':0})):
            with self.subTest(path=path,turn=turn), self.assertRaises(ProviderUnavailable):
                parse_browser_result('mistral',{**result,'request_turn':turn},path,'owned prompt')

    def test_mistral_trailing_chunk_separator_is_not_a_whole_chunk(self):
        bootstrap, root, _, _, success, _, message = self.mistral_patch_fixture()
        chunks = message([{'op':'replace','path':'/contentChunks','value':[
            {'type':'text','text':'first'}, {'type':'text','text':'second'}]}])
        for patch in ({'op':'remove','path':'/contentChunks/0/'},
                      {'op':'add','path':'/contentChunks/0/','value':{'type':'text','text':'inserted'}}):
            with self.subTest(patch=patch), self.assertRaises(ProviderUnavailable):
                mistral_answer(bootstrap+root+chunks+message([patch])+success+b'8:null\n','owned prompt','owned-chat')

    def test_nonfinite_or_unbounded_timeout_is_rejected(self):
        for value in ("nan", "inf", "0", "-1", "1801"):
            with patch.dict(os.environ, {"WEB_PROVIDER_TIMEOUT": value}), self.assertRaises(ValueError):
                completion_timeout()

    def test_cli_only_accepts_explicit_success_with_valid_conversation(self):
        obj = {"status":"SUCCESS","response":"answer","conversation_id":UUID}
        self.assertEqual(parse_result(json.dumps(obj), 0, "flash").text, "answer")
        for changes, code in (({"status":"WAITING"}, 0), ({"error":"quota"}, 0),
                              ({"response":42}, 0), ({"conversation_id":"../private"}, 0), ({}, 1)):
            with self.assertRaises(ProviderUnavailable):
                parse_result(json.dumps({**obj, **changes}), code, "flash")
        with self.assertRaises(ProviderUnavailable):
            parse_result(json.dumps(obj), 0, "flash", "another-conversation")

    def test_cli_prompt_is_stdin_and_workspace_has_no_user_project(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "agy-test"
            script.write_text("#!" + sys.executable + "\n" +
                              "import sys,json,pathlib\n"
                              "print(" + repr(json.dumps(cli_init())) + ",flush=True)\n"
                              "p=json.loads(sys.stdin.read())['message']['content']\n"
                              "assert p=='bridge marker' and p not in sys.argv\n"
                              "assert '--disable-slash-commands' in sys.argv\n"
                              "assert sys.argv[sys.argv.index('--print-timeout')+1].endswith('s')\n"
                              "assert '--dangerously-skip-permissions' not in sys.argv\n"
                              "assert list(pathlib.Path('.').iterdir())==[pathlib.Path('.agents')]\n"
                              "assert 'tools: []' in pathlib.Path('.agents/agents/opencode-text-bridge.md').read_text()\n"
                              "print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':p,'conversation_id':'" + UUID + "'}}))\n")
            script.chmod(0o700)
            with patch.dict(os.environ, {"ANTIGRAVITY_BIN": str(script)}):
                reply = AntigravityClient().chat("bridge marker", model="flash")
            self.assertEqual(reply.text, "bridge marker")

    def test_cli_blocked_stdin_can_be_cancelled_and_process_is_reaped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "agy-test"
            pidfile = root / "pid"
            script.write_text("#!" + sys.executable + "\nimport os,time,pathlib,sys\n"
                              "print(" + repr(json.dumps(cli_init())) + ",flush=True)\n"
                              "sys.stdin.read(1)\n"
                              "pathlib.Path(" + repr(str(pidfile)) + ").write_text(str(os.getpid()))\n"
                              "time.sleep(30)\n")
            script.chmod(0o700)

            def cancelled():
                if pidfile.exists():
                    raise asyncio.CancelledError()

            with patch.dict(os.environ, {"ANTIGRAVITY_BIN":str(script)}), self.assertRaises(asyncio.CancelledError):
                AntigravityClient(cancelled).chat("x" * 1024 * 1024, model="flash")
            pid = int(pidfile.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    @unittest.skipUnless(os.name == 'posix', 'Owned process-group fixture')
    def test_cli_denied_group_probe_still_escalates_and_reaps_owned_process(self):
        import signal
        import subprocess
        from providers.antigravity import stop_process_group
        real_killpg = os.killpg
        process = subprocess.Popen([sys.executable,'-c',
            "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print('ready',flush=True);time.sleep(30)"],
            start_new_session=True,stdout=subprocess.PIPE,text=True)
        signals = []
        def denied_probe(pgid, sig):
            self.assertEqual(pgid, process.pid)
            signals.append(sig)
            if sig == 0:
                raise PermissionError('Owned group liveness probe denied')
            return real_killpg(pgid, sig)
        try:
            self.assertEqual(process.stdout.readline().strip(),'ready')
            with patch('providers.antigravity.os.killpg',side_effect=denied_probe):
                stop_process_group(process)
            self.assertIn(signal.SIGKILL,signals)
            self.assertIsNotNone(process.returncode)
        finally:
            try:
                real_killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()

    @unittest.skipUnless(os.name == 'posix', 'Owned process-group fixture')
    def test_cli_descendant_ignoring_term_does_not_survive_success_cancel_or_timeout(self):
        import signal
        import subprocess
        for mode in ('success','cancel','timeout'):
            from server import api
            api.guard.resume("gemini")
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                pidfile = root / 'child-pid'
                child = "import os,signal,time,pathlib;signal.signal(signal.SIGTERM,signal.SIG_IGN);pathlib.Path(" + repr(str(pidfile)) + ").write_text(str(os.getpid()));time.sleep(30)"
                script = root / 'agy-test'
                script.write_text('#!' + sys.executable + '\nimport sys,json,time,subprocess,pathlib\n'
                    + 'subprocess.Popen([sys.executable,"-c",' + repr(child) + '])\n'
                    + 'while not pathlib.Path(' + repr(str(pidfile)) + ').exists(): time.sleep(.01)\n'
                    + 'print(' + repr(json.dumps(cli_init())) + ',flush=True)\n'
                    + 'sys.stdin.readline()\n'
                    + ('print(' + repr(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'answer','conversation_id':UUID}})) + ',flush=True)\n' if mode == 'success' else 'time.sleep(30)\n'))
                script.chmod(0o700)
                def cancelled():
                    if mode == 'cancel' and pidfile.exists():
                        raise asyncio.CancelledError()
                try:
                    with patch.dict(os.environ, {'ANTIGRAVITY_BIN':str(script),'WEB_PROVIDER_TIMEOUT':'10'}):
                        if mode == 'success':
                            self.assertEqual(AntigravityClient(cancelled).chat('marker',model='flash').text,'answer')
                        else:
                            with self.assertRaises(asyncio.CancelledError if mode == 'cancel' else ProviderUnavailable):
                                AntigravityClient(cancelled).chat('marker',model='flash')
                    pid = int(pidfile.read_text())
                    state = subprocess.run(['ps','-p',str(pid),'-o','stat='],capture_output=True,text=True).stdout.strip()
                    self.assertTrue(not state or state.startswith('Z'), 'Owned descendant remains runnable: ' + state)
                finally:
                    if pidfile.exists():
                        try:
                            os.kill(int(pidfile.read_text()),signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def test_cli_rejects_tool_capabilities_before_sending_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "agy-test"
            received = Path(directory) / "received"
            frame = cli_init()
            frame['init']['tools'] = ['run_command']
            script.write_text("#!" + sys.executable + "\nimport sys,pathlib\n"
                              "print(" + repr(json.dumps(frame)) + ",flush=True)\n"
                              "pathlib.Path(" + repr(str(received)) + ").write_text(sys.stdin.read())\n")
            script.chmod(0o700)
            with patch.dict(os.environ, {"ANTIGRAVITY_BIN":str(script)}), self.assertRaises(ProviderUnavailable):
                AntigravityClient().chat("private prompt", model="flash")
            self.assertFalse(received.exists() and received.read_text())

    def test_cli_stream_needs_verified_init_and_exactly_one_terminal_result(self):
        result = {"event":"result","result":{"status":"SUCCESS","response":"answer","conversation_id":UUID}}
        body = json.dumps(cli_init()) + '\n' + json.dumps(result)
        self.assertEqual(parse_stream(body, 0, "flash").text, "answer")
        tool = {"event":"step_update","step_update":{"step_type":"tool","tool_name":"run_command"}}
        for bad in (json.dumps(result), body+'\n'+json.dumps(result),
                    json.dumps(cli_init())+'\n'+json.dumps(tool)+'\n'+json.dumps(result)):
            with self.assertRaises(ProviderUnavailable):
                parse_stream(bad, 0, "flash")
