import json
import threading
import time
import unittest

import httpx

from deepseek.client import DeepSeekClient, DeepSeekStreamError, _parse_sse
from providers.access import ProviderRejected


def event(value, name=None):
    prefix = f"event: {name}\n" if name else ""
    return prefix + "data: " + json.dumps(value) + "\n\n"


def snapshot(text="Hello", status=None):
    response = {"message_id":2,"fragments":[{"type":"RESPONSE","content":text}]}
    if status:
        response["status"] = status
    return event({"v":{"response":response}})


def parse(payload):
    meta = {}
    answer = "".join(_parse_sse(payload.splitlines(),meta))
    return answer, meta


def mock_client(payload):
    client = DeepSeekClient.__new__(DeepSeekClient)
    client._request_lock = threading.Lock()
    client.create_chat_session = lambda **kwargs: "fake-session"
    client._pow_header = lambda **kwargs: "fake-pow"
    client._http = httpx.Client(base_url="https://chat.deepseek.com",
                               transport=httpx.MockTransport(lambda request: httpx.Response(200,text=payload,headers={"content-type":"text/event-stream"})))
    return client


class StreamProtocolTests(unittest.TestCase):
    def test_thinking_filtered_and_compact_content_appends_preserved(self):
        payload = event({"v":{"response":{"message_id":2,"fragments":[{"type":"THINK","content":"private"}]}}})
        payload += event({"p":"response/fragments/-1/content","o":"APPEND","v":"thought"})
        payload += event({"p":"response/fragments","o":"APPEND","v":[{"type":"RESPONSE","content":"Hello"}]})
        payload += event({"p":"response/fragments/-1/content","o":"APPEND","v":" world"})
        payload += event({"v":"!"}) + "data: [DONE]\n\n"
        answer, meta = parse(payload)
        self.assertEqual(answer,"Hello world!")
        self.assertEqual(meta,{"message_id":2,"finish_reason":"stop"})

    def test_non_content_path_cannot_leak_into_answer(self):
        payload = snapshot() + event({"p":"response/status","o":"SET","v":"RUNNING"})
        payload += event({"v":"metadata-only"})
        payload += event({"p":"response/status","v":"FINISHED"})
        self.assertEqual(parse(payload)[0],"Hello")

    def test_set_operation_is_not_mistaken_for_append(self):
        payload = snapshot() + event({"p":"response/fragments/0/content","o":"SET","v":"replacement"})
        payload += event({"v":"metadata"}) + "data: [DONE]\n\n"
        with self.assertRaises((DeepSeekStreamError, ProviderRejected)):
            parse(payload)

    def test_repeated_snapshot_does_not_duplicate_emitted_text(self):
        payload = snapshot("Hi")
        payload += event({"p":"response/fragments/0/content","o":"APPEND","v":" there"})
        payload += snapshot("Hi there","FINISHED")
        self.assertEqual(parse(payload)[0],"Hi there")

    def test_response_identity_and_emitted_prefix_cannot_change(self):
        other = event({"v": {"response": {"message_id": 4, "fragments": [{"type": "RESPONSE", "content": "Bravo extended"}], "status": "FINISHED"}}})
        for payload in (snapshot("Alpha") + other,
                        snapshot("ABC") + snapshot("XYZmore", "FINISHED"),
                        snapshot() + event({"p": "response/message_id", "o": "SET", "v": 4}) + "data: [DONE]\n\n"):
            with self.subTest(payload=payload), self.assertRaises((DeepSeekStreamError, ProviderRejected)):
                parse(payload)

    def test_damaged_fragments_never_complete_a_partial_response(self):
        for bad in ('not json', [42], False, [{}], [{"type": False}], [{"type": "RESPONSE", "content": False}]):
            payload = snapshot("partial") + event({"p": "response/fragments", "o": "APPEND", "v": bad}) + "data: [DONE]\n\n"
            with self.subTest(bad=bad), self.assertRaises((DeepSeekStreamError, ProviderRejected)):
                parse(payload)

    def test_earlier_fragment_cannot_grow_after_later_text_was_emitted(self):
        for first in ("Hello", ""):
            initial = event({"v": {"response": {"message_id": 2, "fragments": [
                {"type": "RESPONSE", "content": first},
                {"type": "RESPONSE", "content": " world"}]}}})
            snapshot_update = event({"v": {"response": {"message_id": 2, "fragments": [
                {"type": "RESPONSE", "content": first + " dear"},
                {"type": "RESPONSE", "content": " world"}], "status": "FINISHED"}}})
            patch_update = event({"p": "response/fragments/0/content", "o": "APPEND", "v": " dear"})
            for update in (snapshot_update, patch_update):
                with self.subTest(first=first, update=update), self.assertRaises((DeepSeekStreamError, ProviderRejected)):
                    parse(initial + update + "data: [DONE]\n\n")

    def test_last_response_fragment_can_grow_without_reordering(self):
        def fragments(last, status=None):
            response = {"message_id": 2, "fragments": [
                {"type": "RESPONSE", "content": "Hello"},
                {"type": "RESPONSE", "content": last}]}
            if status:
                response["status"] = status
            return event({"v": {"response": response}})
        self.assertEqual(parse(fragments(" world") + fragments(" world dear", "FINISHED"))[0],
                         "Hello world dear")
        self.assertEqual(parse(fragments("") + fragments(" world", "FINISHED"))[0],
                         "Hello world")

    def test_unsupported_fragment_list_patch_cannot_succeed_partially(self):
        for operation in ("SET", "REMOVE", "INSERT", None):
            patch = {"p": "response/fragments", "v": [{"type": "RESPONSE", "content": "replacement"}]}
            if operation is not None:
                patch["o"] = operation
            with self.subTest(operation=operation), self.assertRaises((DeepSeekStreamError, ProviderRejected)):
                parse(snapshot("partial") + event(patch) + "data: [DONE]\n\n")

    def test_eof_and_errors_fail_instead_of_successful_empty_stop(self):
        payloads = [snapshot("partial"), "", event({"error":{"code":403,"message":"token expired"}}),
                    event({"code":1,"msg":"upstream error"}),
                    event({"v":{"error":{"message":"nested error"}}}),
                    event({},"error"), 'data: {broken}\n\n',
                    snapshot() + event({"p":"response/status","v":"FAILED"})]
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises((DeepSeekStreamError, ProviderRejected)):
                parse(payload)

    def test_terminal_markers_and_final_event_without_blank_line(self):
        payloads = [snapshot(status="FINISHED"),snapshot()+event({},"finish"),
                    snapshot()+"event: finish\n\n",snapshot()+"data: [DONE]\n\n",
                    snapshot()+event({"p":"response/status","v":"FINISHED"}).rstrip("\n")]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertEqual(parse(payload),("Hello",{"message_id":2,"finish_reason":"stop"}))

    def test_output_limit_has_length_finish_reason(self):
        self.assertEqual(parse(snapshot("cut","INCOMPLETE")),
                         ("cut",{"message_id":2,"finish_reason":"length"}))

    def test_parent_message_id_patch_does_not_overwrite_assistant_id(self):
        payload = snapshot() + event({"p":"response/parent_message_id","v":99}) + "data: [DONE]\n\n"
        self.assertEqual(parse(payload)[1]["message_id"],2)

    def test_completed_reply_retains_parent_and_finish_reason(self):
        client = mock_client(snapshot("cut","INCOMPLETE"))
        self.addCleanup(client.close)
        reply = client.chat("audit",conversation_id="existing:4")
        self.assertEqual((reply.text,reply.conversation_id,reply.finish_reason),("cut","existing:2","length"))

    def test_abandoned_stream_keeps_original_parent_and_releases_lock(self):
        client = mock_client(snapshot(status="FINISHED"))
        self.addCleanup(client.close)
        stream = client.stream("audit",conversation_id="existing:4")
        iterator = iter(stream)
        self.assertEqual(next(iterator),"Hello")
        iterator.close()
        self.assertEqual(stream.conversation_id,"existing:4")
        self.assertTrue(client._request_lock.acquire(blocking=False))
        client._request_lock.release()
        self.assertEqual(client.chat("audit",conversation_id="existing:4").conversation_id,"existing:2")

    def test_client_serializes_whole_upstream_generations(self):
        client = mock_client(snapshot(status="FINISHED"))
        client._http.close()
        state = {"active":0,"maximum":0}
        guard = threading.Lock()
        barrier = threading.Barrier(3)
        errors = []
        def handle(request):
            with guard:
                state["active"] += 1
                state["maximum"] = max(state["maximum"],state["active"])
            time.sleep(0.02)
            with guard:
                state["active"] -= 1
            return httpx.Response(200,text=snapshot(status="FINISHED"),headers={"content-type":"text/event-stream"})
        client._http = httpx.Client(base_url="https://chat.deepseek.com",transport=httpx.MockTransport(handle))
        self.addCleanup(client.close)
        def chat():
            try:
                barrier.wait(timeout=2)
                client.chat("audit")
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=chat) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=3)
        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(state["maximum"],1)
