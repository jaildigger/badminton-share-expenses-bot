"""Small Telegram Bot API client using Python's standard library."""
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class TelegramError(Exception):
    def __init__(self, code, retry_after=0, reason=""):
        # Never include request URLs: they contain the bot token.
        super().__init__("Telegram API error {}".format(code))
        self.code = code
        self.retry_after = retry_after
        self.reason = reason


class Telegram:
    def __init__(self, token):
        self._base = "https://api.telegram.org/bot" + token + "/"

    def call(self, method, payload=None):
        request = Request(self._base + method, data=json.dumps(payload or {}).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=45) as response:
                result = json.load(response)
        except HTTPError as error:
            try:
                result = json.load(error)
            except (ValueError, OSError):
                raise TelegramError(error.code) from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise TelegramError(0) from None
        if not result.get("ok"):
            description = result.get("description", "").lower()
            reason = ""
            if "message is not modified" in description:
                reason = "not_modified"
            elif "message to edit not found" in description:
                reason = "message_missing"
            raise TelegramError(result.get("error_code", 0), result.get("parameters", {}).get("retry_after", 0), reason)
        return result["result"]
