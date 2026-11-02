"""Regression tests for the interactive login navigation.

The endless-hCaptcha report was caused by login() navigating to CHAT_URL and
then immediately navigating again to SIGNIN_URL when no token was visible yet.
That second navigation can interrupt a human-check. These tests lock in the
"navigate once, then wait" behaviour.
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

from deepseek import auth


def _session() -> auth.Session:
    return auth.Session(
        token="fake-token",
        cookies={},
        user_agent="fake-agent",
        captured_at=time.time(),
    )


class LoginNavigationTests(unittest.TestCase):
    def _run_login(self, *, existing_token, wait_result, capture_result,
                   assume_logged_out=False):
        context, page = MagicMock(), MagicMock()
        context.pages = [page]
        playwright = MagicMock()
        playwright.__enter__.return_value = MagicMock()

        with patch.object(auth, "sync_playwright", return_value=playwright), \
                patch.object(auth, "_launch_context", return_value=context), \
                patch.object(auth, "_safe_goto") as navigate, \
                patch.object(auth, "_safe_evaluate",
                             return_value=existing_token) as evaluate, \
                patch.object(auth, "_wait_for_token",
                             return_value=wait_result) as wait, \
                patch.object(auth, "_capture_from_context",
                             return_value=capture_result), \
                patch.object(auth.Session, "save", autospec=True) as save:
            result = auth.login(
                profile_dir=MagicMock(),
                assume_logged_out=assume_logged_out,
            )
        return {
            "result": result,
            "context": context,
            "page": page,
            "navigate": navigate,
            "evaluate": evaluate,
            "wait": wait,
            "save": save,
        }

    def test_waits_for_token_without_second_navigation(self):
        for logged_out in (False, True):
            with self.subTest(assume_logged_out=logged_out):
                captured = _session()
                calls = self._run_login(
                    existing_token=None,
                    wait_result="fake-token",
                    capture_result=captured,
                    assume_logged_out=logged_out,
                )
                destination = auth.SIGNIN_URL if logged_out else auth.CHAT_URL
                calls["navigate"].assert_called_once_with(calls["page"], destination)
                calls["wait"].assert_called_once_with(calls["page"], timeout=300)
                calls["save"].assert_called_once_with(captured)
                calls["context"].close.assert_called_once_with()
                self.assertIs(calls["result"], captured)

    def test_existing_session_skips_waiting_and_redirect(self):
        captured = _session()
        calls = self._run_login(
            existing_token="fake-token",
            wait_result="unused",
            capture_result=captured,
        )
        calls["navigate"].assert_called_once_with(calls["page"], auth.CHAT_URL)
        calls["wait"].assert_not_called()
        calls["save"].assert_called_once_with(captured)
        calls["context"].close.assert_called_once_with()

    def test_timeout_does_not_retry_navigation_and_closes_window(self):
        context, page = MagicMock(), MagicMock()
        context.pages = [page]
        playwright = MagicMock()
        playwright.__enter__.return_value = MagicMock()

        with patch.object(auth, "sync_playwright", return_value=playwright), \
                patch.object(auth, "_launch_context", return_value=context), \
                patch.object(auth, "_safe_goto") as navigate, \
                patch.object(auth, "_safe_evaluate", return_value=None), \
                patch.object(auth, "_wait_for_token", return_value=None), \
                patch.object(auth, "_capture_from_context") as capture, \
                patch.object(auth.Session, "save", autospec=True) as save:
            with self.assertRaisesRegex(RuntimeError, "Login timed out"):
                auth.login(profile_dir=MagicMock())

        navigate.assert_called_once_with(page, auth.CHAT_URL)
        capture.assert_not_called()
        save.assert_not_called()
        context.close.assert_called_once_with()

    def test_capture_failure_closes_window(self):
        context, page = MagicMock(), MagicMock()
        context.pages = [page]
        playwright = MagicMock()
        playwright.__enter__.return_value = MagicMock()

        with patch.object(auth, "sync_playwright", return_value=playwright), \
                patch.object(auth, "_launch_context", return_value=context), \
                patch.object(auth, "_safe_goto"), \
                patch.object(auth, "_safe_evaluate", return_value="fake-token"), \
                patch.object(auth, "_capture_from_context", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "could not read the token"):
                auth.login(profile_dir=MagicMock())

        context.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
