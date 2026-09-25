"""Fresh membership checks; never grant access when Telegram is unavailable."""
from .calculator import ValidationError
from .telegram import TelegramError


class Membership:
    def __init__(self, api, bot_id):
        self.api = api
        self.bot_id = bot_id

    def __call__(self, chat_id, uid):
        try:
            bot = self.api.call("getChatMember", {"chat_id": chat_id, "user_id": self.bot_id})
            if bot.get("status") != "administrator":
                raise ValidationError("Попросите администратора группы назначить бота администратором для проверки участников.")
            member = self.api.call("getChatMember", {"chat_id": chat_id, "user_id": uid})
        except TelegramError as error:
            if error.code in (400, 403):
                raise ValidationError("Не удалось проверить членство. Проверьте, что вы состоите в подключённой группе, а бот назначен её администратором.")
            raise
        return member.get("status") in ("creator", "administrator", "member") or (
            member.get("status") == "restricted" and member.get("is_member") is True)
