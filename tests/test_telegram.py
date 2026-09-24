import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from badminton.telegram import Telegram, TelegramError


class TelegramTests(unittest.TestCase):
    def test_posts_json_and_parses_response(self):
        with patch("badminton.telegram.urlopen", return_value=io.BytesIO(b'{"ok":true,"result":[]}')) as request:
            self.assertEqual(Telegram("test-secret").call("getUpdates", {"offset": 7, "timeout": 30}), [])
            sent = request.call_args[0][0]
            self.assertEqual(sent.method, "POST")
            self.assertEqual(json.loads(sent.data), {"offset": 7, "timeout": 30})
            self.assertEqual(request.call_args[1]["timeout"], 45)

    def test_rate_limit_preserves_retry_after_but_not_token(self):
        error = HTTPError("https://example/bottest-secret", 429, "rate limited", {},
                          io.BytesIO(b'{"ok":false,"error_code":429,"parameters":{"retry_after":3}}'))
        with patch("badminton.telegram.urlopen", side_effect=error):
            with self.assertRaises(TelegramError) as caught:
                Telegram("test-secret").call("sendMessage")
            self.assertEqual(caught.exception.retry_after, 3)
            self.assertEqual(caught.exception.code, 429)
            self.assertNotIn("test-secret", str(caught.exception))

    def test_network_failure_redacts_url(self):
        with patch("badminton.telegram.urlopen", side_effect=URLError("https://example/bottest-secret")):
            with self.assertRaises(TelegramError) as caught:
                Telegram("test-secret").call("getMe")
            self.assertEqual(caught.exception.code, 0)
            self.assertNotIn("test-secret", str(caught.exception))

    def test_invalid_json_is_retryable(self):
        with patch("badminton.telegram.urlopen", return_value=io.BytesIO(b"not json")):
            with self.assertRaises(TelegramError) as caught:
                Telegram("test-secret").call("getUpdates")
            self.assertEqual(caught.exception.code, 0)
