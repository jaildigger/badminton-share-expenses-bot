import argparse
import json
import logging
import os
import re
import time
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from .bot import Bot
from .store import Store
from .telegram import Telegram, TelegramError
from . import publishing
from . import polls
from .access import Membership


def load_env(path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z_][A-Z_0-9]*", key.strip()):
            raise ValueError("Некорректная строка в .env")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def drain_outbox(store, api):
    while True:
        row = store.one("SELECT * FROM outbox ORDER BY id LIMIT 1")
        if not row:
            return
        try:
            payload = json.loads(row["payload"])
            if row["method"] == "publishTraining":
                publishing.deliver(store, api, payload["training_id"])
            elif row["method"] == "sendAttendancePoll":
                polls.deliver(store, api, payload["id"])
            else:
                api.call(row["method"], payload)
        except TelegramError as error:
            # Expired callback acknowledgements and blocked/deleted chats are terminal.
            if row["method"] == "publishTraining" and error.code in (400, 403):
                with store.db:
                    publishing.notify_failure(store, payload["training_id"])
            elif (row["method"] == "answerCallbackQuery" and error.code == 400) or error.code == 403:
                logging.warning("Ответ не доставлен: %s, code=%s", row["method"], error.code)
            else:
                raise
        with store.db:
            store.execute("DELETE FROM outbox WHERE id=?", (row["id"],))


def main():
    parser = argparse.ArgumentParser(description="Бот расчёта тренировок по бадминтону")
    parser.add_argument("--check", action="store_true", help="Проверить настройки без подключения к Telegram")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        load_env(Path(".env"))
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("Укажите TELEGRAM_BOT_TOKEN в .env (токен из @BotFather).")
        admins = {int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
        if not admins or any(uid <= 0 for uid in admins):
            raise ValueError("Укажите ADMIN_IDS в .env — числовые Telegram ID организаторов через запятую.")
        miniapp_url = os.environ.get("MINIAPP_URL", "").strip()
        miniapp_port = int(os.environ.get("MINIAPP_PORT", "8080"))
        if not 1 <= miniapp_port <= 65535:
            raise ValueError("MINIAPP_PORT должен быть от 1 до 65535.")
        if miniapp_url:
            url = urlsplit(miniapp_url)
            if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
                raise ValueError("MINIAPP_URL должен быть HTTPS-адресом без пути, параметров и пароля.")
    except ValueError as error:
        parser.exit(2, "Ошибка настроек: {}\n".format(error))
    if args.check:
        print("Настройки корректны. Проверка токена в Telegram не выполнялась.")
        return
    path = Path(os.environ.get("DATABASE_PATH", "data/badminton.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    # One poller per DB. OS releases this lock automatically on exit/crash.
    import fcntl
    lock = path.with_suffix(".lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.exit(2, "Бот с этой базой уже запущен.\n")
    store = Store(path)
    if store.setting("shared_workspace") != "1":
        backup_path = path.with_name(path.stem + ".before-shared-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".sqlite3")
        with sqlite3.connect(str(backup_path)) as backup:
            store.db.backup(backup)
        store.initialize_shared()
    store.seed_defaults({0})
    bot = Bot(store, admins)
    api = Telegram(token)
    web_server = None
    try:
        me = api.call("getMe")
        bot.username = me["username"]
        bot.membership = Membership(api, me["id"])
        webhook = api.call("getWebhookInfo")
        if webhook.get("url"):
            parser.exit(2, "У бота активен webhook. Отключите прежний способ запуска перед long polling.\n")
        if miniapp_url:
            from .miniapp import start_server
            web_server = start_server(path, token, admins, bot.membership,
                                      os.environ.get("MINIAPP_HOST", "127.0.0.1"), miniapp_port)
            api.call("setChatMenuButton", {"menu_button": {
                "type": "web_app", "text": "Тренировки", "web_app": {"url": miniapp_url}}})
            logging.info("Mini App слушает порт %s", miniapp_port)
        logging.info("Бот @%s запущен. Организаторов: %s", me["username"], len(admins))
        delay = 1
        while True:
            try:
                drain_outbox(store, api)
                updates = api.call("getUpdates", {"offset": int(store.setting("offset", "0")), "timeout": 30,
                                                   "allowed_updates": ["message", "callback_query", "poll", "poll_answer"]})
                for update in updates:
                    bot.process(update)
                    drain_outbox(store, api)
                delay = 1
            except TelegramError as error:
                if error.code in (401, 404, 409):
                    logging.error("Проверьте токен и отсутствие второго процесса бота. Code=%s", error.code)
                    return 1
                logging.warning("Telegram временно недоступен. Code=%s; повтор запроса.", error.code)
                time.sleep(max(delay, error.retry_after))
                delay = min(delay*2, 30)
    except TelegramError as error:
        logging.error("Не удалось подключиться к Telegram. Code=%s", error.code)
        return 1
    except KeyboardInterrupt:
        logging.info("Бот остановлен.")
    finally:
        if web_server:
            web_server.shutdown()
            web_server.server_close()
        store.db.close()
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
