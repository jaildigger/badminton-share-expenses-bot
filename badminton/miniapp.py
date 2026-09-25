"""Telegram Mini App API and static UI, sharing the bot's SQLite/domain layer."""
import hashlib
import hmac
import json
import logging
import sqlite3
import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .bot import Bot
from .calculator import ValidationError, hours_input, money_input, quantity_input
from .publishing import queue_updates
from .store import Store
from .telegram import TelegramError


class AuthenticationError(ValueError):
    pass


class Conflict(ValidationError):
    pass


def validate_init_data(raw, token, now=None):
    """Validate Telegram's signed data; never trust initDataUnsafe or a client user ID."""
    try:
        pairs = parse_qsl(raw, strict_parsing=True, max_num_fields=30)
        fields = dict(pairs)
        if len(pairs) != len(fields):
            raise ValueError()
        signature = fields.pop("hash")
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        check = "\n".join("{}={}".format(k, v) for k, v in sorted(fields.items()))
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        age = (time.time() if now is None else now) - int(fields["auth_date"])
        if age < -30 or age > 86400:
            raise ValueError()
        user = json.loads(fields["user"])
        if type(user["id"]) is not int or user["id"] <= 0:
            raise ValueError()
        return user["id"]
    except (ValueError, KeyError, TypeError):
        raise AuthenticationError("Откройте приложение заново через меню бота в Telegram.") from None


def snapshot(store, uid, admins, tid=0):
    group = store.one("SELECT title,chat_id,thread_id FROM group_bindings WHERE owner=0")
    data = {
        "revision": store.revision(), "is_admin": uid in admins,
        "today": datetime.now(timezone(timedelta(hours=7))).date().isoformat(),
        "group": dict(group) if group else None,
        "people": [dict(r) for r in store.all("SELECT id,name FROM people WHERE owner=0 AND saved=1 ORDER BY name COLLATE NOCASE")],
        "locations": [dict(r) for r in store.all("SELECT id,name FROM locations WHERE owner=0 AND active=1 ORDER BY name")],
        "trainings": [dict(r) for r in store.all("""SELECT t.*,l.name location,
            (SELECT COUNT(*) FROM participants WHERE training_id=t.id) participants_count
            FROM trainings t JOIN locations l ON l.id=t.location_id WHERE t.owner=0 ORDER BY t.date DESC,t.id DESC""")],
    }
    if tid:
        t = dict(store.training(0, tid))
        t["participants"] = [dict(r) for r in store.participants(tid)]
        t["payers"] = [dict(r) for r in store.all("SELECT c.*,p.name FROM court_payments c JOIN people p ON p.id=c.person_id WHERE training_id=?", (tid,))]
        t["transfers"] = [dict(r) for r in store.transfers(tid)]
        pub = store.one("SELECT title,chat_id,thread_id FROM publications WHERE training_id=?", (tid,))
        t["publication"] = dict(pub) if pub else None
        try:
            result = store.calculation(0, tid)
            t["calculation"] = asdict(result)
            ids = set(result.balances)
            t["names"] = {str(pid): store.person_owned(0, pid)["name"] for pid in ids}
        except ValidationError as error:
            t["calculation_error"] = str(error)
        data["training"] = t
    return data


def mutate(store, uid, body):
    """Called in a BEGIN IMMEDIATE transaction, with a checked shared revision."""
    action = body["action"]
    tid = int(body.get("tid", 0))
    if action in ("person", "location"):
        if action == "person":
            store.person(0, body["name"])
        else:
            store.location(0, body["name"])
        return tid
    if action in ("create", "details"):
        if action == "details":
            store.training(0, tid, draft=True)
        date = Bot.parse_date(body["date"])
        cost = money_input(body["court_cost"])
        lid = store.location(0, body["location_name"]) if body.get("location_name") else int(body["location_id"])
        if not store.one("SELECT 1 FROM locations WHERE id=? AND owner=0 AND active=1", (lid,)):
            raise ValidationError("Выберите локацию.")
        if action == "create":
            return store.execute("INSERT INTO trainings(owner,date,location_id,court_cost,created_by) VALUES (0,?,?,?,?)", (date, lid, cost, uid)).lastrowid
        paid = store.one("SELECT COALESCE(SUM(amount),0) FROM court_payments WHERE training_id=?", (tid,))[0]
        if cost < paid:
            raise ValidationError("Сначала уменьшите оплаты корта: они превышают новую стоимость.")
        store.execute("UPDATE trainings SET date=?,location_id=?,court_cost=? WHERE id=?", (date, lid, cost, tid))
    elif action in ("add", "participant", "remove", "payer"):
        store.training(0, tid, draft=True)
        pid = (store.person(0, body["name"], body.get("saved", True) is True)
               if action in ("add", "payer") and body.get("name") else int(body["pid"]))
        store.person_owned(0, pid)
        if action == "add":
            store.add_participant(0, tid, pid)
        elif action == "participant":
            for field, parser, key in (("minutes", hours_input, "hours"), ("shuttle_count", quantity_input, "shuttle_count"), ("shuttle_price", money_input, "shuttle_price")):
                store.update_participant(0, tid, pid, field, parser(body[key]))
        elif action == "remove":
            store.execute("DELETE FROM participants WHERE training_id=? AND person_id=?", (tid, pid))
        else:
            store.set_court_payment(0, tid, pid, money_input(body["amount"]))
    elif action in ("lock", "unlock", "close"):
        {"lock": store.lock, "unlock": store.unlock, "close": store.close_training}[action](0, tid)
    elif action == "reopen":
        if store.training(0, tid)["status"] != "closed":
            raise ValidationError("Тренировка не закрыта.")
        store.execute("UPDATE trainings SET status='settling' WHERE id=?", (tid,))
    elif action in ("pay", "undo"):
        transfer_id = int(body["transfer_id"])
        tr = store.transfer(0, transfer_id)
        if tr["training_id"] != tid:
            raise ValidationError("Перевод не относится к этой тренировке.")
        if action == "undo":
            store.undo_payment(0, transfer_id)
        else:
            amount = tr["amount"] - tr["paid"] if body.get("full") is True else money_input(body["amount"])
            store.pay(0, transfer_id, amount)
    elif action == "publish":
        if store.training(0, tid)["status"] == "draft":
            raise ValidationError("Сначала зафиксируйте расчёт.")
        pub = store.one("SELECT * FROM publications WHERE training_id=?", (tid,))
        group = pub or store.one("SELECT * FROM group_bindings WHERE owner=0")
        if not group:
            raise ValidationError("Сначала подключите группу командой /bind.")
        if body.get("chat_id") != group["chat_id"] or body.get("thread_id") != group["thread_id"]:
            raise Conflict("Группа или тема изменилась. Обновите данные и подтвердите публикацию снова.")
        if not pub:
            store.execute("INSERT INTO publications(training_id,chat_id,title,thread_id) VALUES (?,?,?,?)", (tid, group["chat_id"], group["title"], group["thread_id"]))
        queue_updates(store, 0, force_tid=tid, notify_uid=uid)
    else:
        raise ValidationError("Неизвестное действие.")
    queue_updates(store, 0, notify_uid=uid)
    return tid


def dispatch(store, uid, admins, body):
    with store.db:
        store.execute("BEGIN IMMEDIATE")
        key = body.get("request_id", "")
        if not isinstance(key, str) or not 16 <= len(key) <= 100:
            raise ValidationError("Некорректный идентификатор запроса.")
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        previous = store.one("SELECT * FROM miniapp_requests WHERE uid=? AND request_id=?", (uid, key))
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise Conflict("Этот запрос уже использован. Обновите данные.")
            return snapshot(store, uid, admins, previous["tid"])
        if body.get("revision") != store.revision():
            raise Conflict("Данные уже изменились. Нажмите «Обновить» и повторите действие.")
        tid = mutate(store, uid, body)
        store.execute("INSERT INTO miniapp_requests VALUES (?,?,?,?,?)", (uid, key, fingerprint, tid, int(time.time())))
        return snapshot(store, uid, admins, tid)


def initialize_api(store):
    with store.db:
        store.execute("""CREATE TABLE IF NOT EXISTS miniapp_requests (
            uid INTEGER NOT NULL,request_id TEXT NOT NULL,fingerprint TEXT NOT NULL,
            tid INTEGER NOT NULL,created_at INTEGER NOT NULL,PRIMARY KEY(uid,request_id))""")


def make_server(path, token, admins, membership, host="127.0.0.1", port=8080):
    # Initialized before serving, after the normal shared-workspace migration.
    setup = Store(path)
    initialize_api(setup)
    setup.db.close()
    static = Path(__file__).with_name("web")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # Do not log URLs/headers that could contain authentication data.

        def reply(self, status, value, mime="application/json; charset=utf-8"):
            payload = json.dumps(value, ensure_ascii=False).encode() if not isinstance(value, bytes) else value
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            route = urlsplit(self.path).path
            files = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}
            if route in files:
                name, mime = files[route]
                return self.reply(200, (static / name).read_bytes(), mime + "; charset=utf-8")
            if route == "/api/state":
                return self.api(False)
            self.reply(404, {"error": "Страница не найдена."})

        def do_POST(self):
            if self.path != "/api/action":
                return self.reply(404, {"error": "Страница не найдена."})
            self.api(True)

        def api(self, write):
            store = None
            try:
                uid = validate_init_data(self.headers.get("X-Telegram-Init-Data", ""), token)
                store = Store(path)
                denial = Bot(store, admins, membership).access_error(uid)
                if denial:
                    return self.reply(403, {"error": denial})
                if write:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 32768 or self.headers.get_content_type() != "application/json":
                        return self.reply(400, {"error": "Некорректный запрос."})
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise ValueError()
                    result = dispatch(store, uid, admins, body)
                else:
                    query = dict(parse_qsl(urlsplit(self.path).query))
                    with store.db:
                        store.execute("BEGIN")
                        result = snapshot(store, uid, admins, int(query.get("tid", 0)))
                self.reply(200, result)
            except AuthenticationError as error:
                self.reply(401, {"error": str(error)})
            except Conflict as error:
                self.reply(409, {"error": str(error)})
            except ValidationError as error:
                self.reply(400, {"error": str(error)})
            except (ValueError, TypeError, KeyError, OverflowError):
                self.reply(400, {"error": "Проверьте заполнение полей."})
            except (TelegramError, sqlite3.OperationalError):
                self.reply(503, {"error": "Сервис временно недоступен. Повторите запрос позже."})
            except Exception:
                logging.exception("Ошибка Mini App")
                self.reply(500, {"error": "Не удалось выполнить действие. Обновите данные."})
            finally:
                if store:
                    store.db.close()

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def start_server(*args, **kwargs):
    server = make_server(*args, **kwargs)
    threading.Thread(target=server.serve_forever, daemon=True, name="miniapp").start()
    return server
