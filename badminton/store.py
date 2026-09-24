"""SQLite persistence. Controller processes each incoming update atomically."""
import json
import sqlite3
from pathlib import Path

from .calculator import Participant, ValidationError, calculate
from .defaults import LOCATIONS, PARTICIPANTS


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS people (
 id INTEGER PRIMARY KEY, owner INTEGER NOT NULL, name TEXT NOT NULL,
 name_key TEXT NOT NULL, saved INTEGER NOT NULL DEFAULT 1);
CREATE UNIQUE INDEX IF NOT EXISTS saved_person ON people(owner,name_key) WHERE saved=1;
CREATE TABLE IF NOT EXISTS locations (
 id INTEGER PRIMARY KEY, owner INTEGER NOT NULL, name TEXT NOT NULL,
 name_key TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, UNIQUE(owner,name_key));
CREATE TABLE IF NOT EXISTS trainings (
 id INTEGER PRIMARY KEY, owner INTEGER NOT NULL, date TEXT NOT NULL,
 location_id INTEGER NOT NULL REFERENCES locations(id), court_cost INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','settling','closed')));
CREATE TABLE IF NOT EXISTS participants (
 training_id INTEGER REFERENCES trainings(id), person_id INTEGER REFERENCES people(id),
 minutes INTEGER NOT NULL DEFAULT 120 CHECK(minutes>0),
 shuttle_count INTEGER NOT NULL DEFAULT 0 CHECK(shuttle_count>=0),
 shuttle_price INTEGER NOT NULL DEFAULT 0 CHECK(shuttle_price>=0),
 PRIMARY KEY(training_id,person_id));
CREATE TABLE IF NOT EXISTS court_payments (
 training_id INTEGER REFERENCES trainings(id), person_id INTEGER REFERENCES people(id),
 amount INTEGER NOT NULL CHECK(amount>=0), PRIMARY KEY(training_id,person_id));
CREATE TABLE IF NOT EXISTS transfers (
 id INTEGER PRIMARY KEY AUTOINCREMENT, training_id INTEGER REFERENCES trainings(id),
 sender INTEGER REFERENCES people(id), recipient INTEGER REFERENCES people(id),
 amount INTEGER NOT NULL CHECK(amount>0));
CREATE TABLE IF NOT EXISTS payments (
 id INTEGER PRIMARY KEY, transfer_id INTEGER REFERENCES transfers(id),
 amount INTEGER NOT NULL CHECK(amount>0), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 voided INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sessions (
 owner INTEGER PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (
 id INTEGER PRIMARY KEY, method TEXT NOT NULL, payload TEXT NOT NULL);
"""


def clean_name(value):
    value = " ".join(value.split())
    if not value or len(value) > 60 or any(ord(c) < 32 for c in value):
        raise ValidationError("Введите название длиной от 1 до 60 символов.")
    return value


class Store:
    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        if "active" not in {r[1] for r in self.db.execute("PRAGMA table_info(locations)")}:
            self.db.execute("ALTER TABLE locations ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        self.db.execute("PRAGMA journal_mode=WAL")

    def one(self, sql, args=()):
        return self.db.execute(sql, args).fetchone()

    def seed_defaults(self, owners):
        """Add missing saved entries without replacing existing IDs or records."""
        with self.db:
            for owner in sorted(set(owners)):
                for name in PARTICIPANTS:
                    self.person(owner, name)
                for name in LOCATIONS:
                    self.location(owner, name)

    def replace_directories_with_defaults(self, owners):
        """Replace directories; referenced records remain only in training history."""
        with self.db:
            for owner in sorted(set(owners)):
                self.execute("UPDATE people SET saved=0 WHERE owner=?", (owner,))
                self.execute("""DELETE FROM people WHERE owner=?
                    AND id NOT IN (SELECT person_id FROM participants)
                    AND id NOT IN (SELECT person_id FROM court_payments)
                    AND id NOT IN (SELECT sender FROM transfers)
                    AND id NOT IN (SELECT recipient FROM transfers)""", (owner,))
                self.execute("UPDATE locations SET active=0 WHERE owner=?", (owner,))
                self.execute("DELETE FROM locations WHERE owner=? AND id NOT IN (SELECT location_id FROM trainings)", (owner,))
                for name in PARTICIPANTS:
                    self.person(owner, name)
                for name in LOCATIONS:
                    self.location(owner, name)

    def all(self, sql, args=()):
        return self.db.execute(sql, args).fetchall()

    def execute(self, sql, args=()):
        return self.db.execute(sql, args)

    def setting(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return row[0] if row else default

    def set_setting(self, key, value):
        self.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, str(value)))

    def enqueue(self, method, payload):
        self.execute("INSERT INTO outbox(method,payload) VALUES (?,?)", (method, json.dumps(payload, ensure_ascii=False)))

    def session(self, owner):
        self.execute("INSERT OR IGNORE INTO sessions(owner) VALUES (?)", (owner,))
        row = self.one("SELECT * FROM sessions WHERE owner=?", (owner,))
        return row["generation"], json.loads(row["state"])

    def state(self, owner, value):
        self.execute("UPDATE sessions SET state=? WHERE owner=?", (json.dumps(value), owner))

    def person(self, owner, name, saved=True):
        name = clean_name(name)
        if saved:
            existing = self.one("SELECT id FROM people WHERE owner=? AND name_key=? AND saved=1", (owner, name.casefold()))
            if existing:
                return existing[0]
        return self.execute("INSERT INTO people(owner,name,name_key,saved) VALUES (?,?,?,?)",
                            (owner, name, name.casefold(), int(saved))).lastrowid

    def location(self, owner, name):
        name = clean_name(name)
        self.execute("INSERT OR IGNORE INTO locations(owner,name,name_key) VALUES (?,?,?)", (owner, name, name.casefold()))
        self.execute("UPDATE locations SET active=1 WHERE owner=? AND name_key=?", (owner, name.casefold()))
        return self.one("SELECT id FROM locations WHERE owner=? AND name_key=?", (owner, name.casefold()))[0]

    def training(self, owner, tid, draft=False):
        row = self.one("SELECT t.*,l.name location FROM trainings t JOIN locations l ON l.id=t.location_id WHERE t.id=? AND t.owner=?", (tid, owner))
        if not row:
            raise ValidationError("Тренировка не найдена.")
        if draft and row["status"] != "draft":
            raise ValidationError("Расчёт зафиксирован. Сначала верните тренировку к редактированию.")
        return row

    def person_owned(self, owner, pid):
        row = self.one("SELECT * FROM people WHERE owner=? AND id=?", (owner, pid))
        if not row:
            raise ValidationError("Участник не найден.")
        return row

    def participants(self, tid):
        return self.all("SELECT p.*,n.name,n.saved FROM participants p JOIN people n ON n.id=p.person_id WHERE training_id=? ORDER BY person_id", (tid,))

    def add_participant(self, owner, tid, pid):
        self.training(owner, tid, draft=True)
        self.person_owned(owner, pid)
        self.execute("INSERT OR IGNORE INTO participants(training_id,person_id) VALUES (?,?)", (tid, pid))

    def update_participant(self, owner, tid, pid, field, value):
        self.training(owner, tid, draft=True)
        if field not in ("minutes", "shuttle_count", "shuttle_price"):
            raise ValidationError("Неизвестное поле.")
        if not self.one("SELECT 1 FROM participants WHERE training_id=? AND person_id=?", (tid, pid)):
            raise ValidationError("Участник не входит в тренировку.")
        self.execute("UPDATE participants SET " + field + "=? WHERE training_id=? AND person_id=?", (value, tid, pid))

    def set_court_payment(self, owner, tid, pid, amount):
        training = self.training(owner, tid, draft=True)
        self.person_owned(owner, pid)
        other = self.one("SELECT COALESCE(SUM(amount),0) FROM court_payments WHERE training_id=? AND person_id<>?", (tid, pid))[0]
        if amount < 0 or other + amount > training["court_cost"]:
            raise ValidationError("Сумма оплат превышает стоимость корта. Сначала исправьте прежние оплаты или стоимость.")
        self.execute("INSERT OR REPLACE INTO court_payments VALUES (?,?,?)", (tid, pid, amount))

    def calculation(self, owner, tid):
        t = self.training(owner, tid)
        participants = [Participant(p["person_id"], p["minutes"], p["shuttle_count"], p["shuttle_price"]) for p in self.participants(tid)]
        payments = {p["person_id"]: p["amount"] for p in self.all("SELECT * FROM court_payments WHERE training_id=?", (tid,))}
        return calculate(t["court_cost"], participants, payments)

    def lock(self, owner, tid):
        self.training(owner, tid, draft=True)
        result = self.calculation(owner, tid)
        for tr in result.transfers:
            self.execute("INSERT INTO transfers(training_id,sender,recipient,amount) VALUES (?,?,?,?)", (tid, tr.sender, tr.recipient, tr.amount))
        self.execute("UPDATE trainings SET status='settling' WHERE id=?", (tid,))

    def transfers(self, tid):
        return self.all("""SELECT tr.*,a.name sender_name,b.name recipient_name,
          COALESCE((SELECT SUM(amount) FROM payments WHERE transfer_id=tr.id AND voided=0),0) paid
          FROM transfers tr JOIN people a ON a.id=tr.sender JOIN people b ON b.id=tr.recipient
          WHERE tr.training_id=? ORDER BY tr.id""", (tid,))

    def transfer(self, owner, transfer_id):
        tr = self.one("SELECT * FROM transfers WHERE id=?", (transfer_id,))
        if not tr:
            raise ValidationError("Перевод не найден.")
        t = self.training(owner, tr["training_id"])
        if t["status"] != "settling":
            raise ValidationError("Для отметки оплаты откройте расчёт тренировки.")
        return next(r for r in self.transfers(t["id"]) if r["id"] == transfer_id)

    def pay(self, owner, transfer_id, amount):
        tr = self.transfer(owner, transfer_id)
        if amount <= 0 or amount > tr["amount"] - tr["paid"]:
            raise ValidationError("Сумма должна быть больше нуля и не превышать остаток перевода.")
        self.execute("INSERT INTO payments(transfer_id,amount) VALUES (?,?)", (transfer_id, amount))

    def undo_payment(self, owner, transfer_id):
        self.transfer(owner, transfer_id)
        row = self.one("SELECT id FROM payments WHERE transfer_id=? AND voided=0 ORDER BY id DESC LIMIT 1", (transfer_id,))
        if not row:
            raise ValidationError("Нет оплаты для отмены.")
        self.execute("UPDATE payments SET voided=1 WHERE id=?", (row[0],))

    def unlock(self, owner, tid):
        t = self.training(owner, tid)
        if t["status"] != "settling":
            raise ValidationError("Тренировка уже редактируется или закрыта.")
        if any(tr["paid"] for tr in self.transfers(tid)):
            raise ValidationError("Сначала отмените отметки переводов. Иначе изменение расходов повлияет на уже оплаченные долги.")
        self.execute("DELETE FROM payments WHERE transfer_id IN (SELECT id FROM transfers WHERE training_id=?)", (tid,))
        self.execute("DELETE FROM transfers WHERE training_id=?", (tid,))
        self.execute("UPDATE trainings SET status='draft' WHERE id=?", (tid,))

    def close_training(self, owner, tid):
        t = self.training(owner, tid)
        if t["status"] != "settling" or any(tr["amount"] != tr["paid"] for tr in self.transfers(tid)):
            raise ValidationError("Сначала отметьте все переводы как оплаченные.")
        self.execute("UPDATE trainings SET status='closed' WHERE id=?", (tid,))
