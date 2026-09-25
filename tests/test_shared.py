import json
import unittest

from badminton.access import Membership
from badminton.bot import Bot
from badminton.calculator import ValidationError
from badminton.store import Store
from badminton.telegram import TelegramError
from tests import test_bot


class SharedWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.h = test_bot.BotTests()
        self.h.setUp()
        self.addCleanup(self.h.tearDown)
        self.s = self.h.store
        self.tid, self.alice, self.bob = self.h.fixture()
        self.s.initialize_shared()
        with self.s.db:
            self.s.execute("INSERT INTO group_bindings VALUES (0,-100,'Бадминтон',NULL)")
        self.members = {22, 33}
        self.h.bot = Bot(self.s, {11}, lambda chat, uid: chat == -100 and uid in self.members)

    def click(self, uid, label):
        row = self.s.one("SELECT payload FROM outbox WHERE method='sendMessage' AND json_extract(payload,'$.chat_id')=? AND json_extract(payload,'$.reply_markup') IS NOT NULL ORDER BY id DESC LIMIT 1", (uid,))
        payload = json.loads(row[0])
        data = next(b["callback_data"] for line in payload["reply_markup"]["inline_keyboard"] for b in line if b["text"] == label)
        return self.h.callback(data, uid)

    def last(self, uid):
        return json.loads(self.s.one("SELECT payload FROM outbox WHERE method='sendMessage' AND json_extract(payload,'$.chat_id')=? ORDER BY id DESC LIMIT 1", (uid,))[0])["text"]

    def test_member_sees_existing_history_and_creates_shared_training(self):
        self.h.message("/history", 22)
        self.assertIn("Тренировки", self.last(22))
        self.h.message("/new", 22)
        self.h.message("26.09.2026", 22)
        self.click(22, "Grand Baam")
        self.h.message("200", 22)
        t = self.s.one("SELECT * FROM trainings ORDER BY id DESC LIMIT 1")
        self.assertEqual((t["owner"], t["created_by"]), (0, 22))
        self.h.message("/history", 33)
        self.click(33, "2026-09-26 · Grand Baam · черновик")
        self.assertIn("200.00", self.last(33))
        self.click(33, "Участники / часы / воланы")
        self.click(33, "Выбрать из сохранённых")
        self.click(33, "Андрей")
        self.assertEqual(self.s.participants(t["id"])[0]["person_id"], self.alice)

    def test_new_person_and_location_shared_across_members(self):
        self.h.message("/start", 22)
        self.click(22, "Добавить участника в справочник")
        self.h.message("Новый участник", 22)
        self.click(22, "Добавить локацию")
        self.h.message("Новый зал", 22)
        self.assertEqual(self.s.one("SELECT owner FROM people WHERE name='Новый участник'")[0], 0)
        self.h.message("/new", 33)
        self.h.message("26.09.2026", 33)
        self.click(33, "Новый зал")
        self.h.message("0", 33)
        self.click(33, "Выбрать из сохранённых")
        self.click(33, "Новый участник")

    def test_outsider_and_departed_member_cannot_mutate(self):
        self.h.message("/start", 99)
        self.assertIn("Доступ только", self.last(99))
        self.h.message("/start", 22)
        self.click(22, "Добавить участника в справочник")
        self.members.remove(22)
        self.h.message("Нельзя добавить", 22)
        self.assertIsNone(self.s.one("SELECT id FROM people WHERE name='Нельзя добавить'"))
        self.assertIn("Доступ только", self.last(22))

    def test_concurrent_edits_and_payment_clicks_are_rejected(self):
        for uid in (22, 33):
            self.h.message("/history", uid)
            self.click(uid, "2026-09-25 · Grand Baam · черновик")
            self.click(uid, "Участники / часы / воланы")
            self.click(uid, "Рамиля · 2 ч")
            self.click(uid, "Изменить время")
        self.h.message("1", 22)
        self.h.message("3", 33)
        self.assertIn("другой участник", self.last(33))
        self.assertEqual(self.s.one("SELECT minutes FROM participants WHERE person_id=?", (self.bob,))[0], 60)
        with self.s.db:
            self.s.lock(0, self.tid)
            tr = self.s.transfers(self.tid)[0]
            self.h.bot.transfer_view(22, tr["id"])
            self.h.bot.transfer_view(33, tr["id"])
        self.click(22, "Отметить остаток оплаченным")
        self.click(33, "Отметить остаток оплаченным")
        self.assertIn("другой участник", self.last(33))
        self.assertEqual(self.s.one("SELECT COUNT(*) FROM payments")[0], 1)

    def test_member_publishes_to_shared_group_notification_goes_to_actor(self):
        with self.s.db:
            self.s.lock(0, self.tid)
            self.h.bot.settlements(22, self.tid)
        self.click(22, "Опубликовать итог в группе")
        self.click(22, "Опубликовать в «Бадминтон»")
        pub = self.s.one("SELECT * FROM publications")
        self.assertEqual((pub["chat_id"], pub["notify_uid"]), (-100, 22))

    def test_only_owner_can_bind_and_cannot_switch_shared_group(self):
        for uid in (22, 11):
            self.h.bot.process({"update_id": self.h.update_id, "message": {"from": {"id": uid}, "chat": {"id": -200, "type": "supergroup", "title": "Другие"}, "text": "/bind@badminton_share_bot"}})
            self.h.update_id += 1
        self.assertEqual(self.s.one("SELECT chat_id FROM group_bindings")[0], -100)

    def test_membership_network_failure_does_not_advance_offset(self):
        def unavailable(*args):
            raise TelegramError(0)
        self.h.bot.membership = unavailable
        before = self.s.setting("offset")
        with self.assertRaises(TelegramError):
            self.h.message("/new", 22)
        self.assertEqual(self.s.setting("offset"), before)

    def test_migration_keeps_ids_amounts_and_is_idempotent(self):
        self.assertEqual(self.s.training(0, self.tid)["created_by"], 11)
        result = self.s.calculation(0, self.tid)
        self.s.initialize_shared()
        self.assertEqual(self.s.calculation(0, self.tid), result)
        self.assertEqual([p["person_id"] for p in self.s.participants(self.tid)], [self.alice, self.bob])


class MigrationTests(unittest.TestCase):
    def test_deduplicate_directories_keep_historical_people_and_payments(self):
        s = Store(":memory:")
        self.addCleanup(s.db.close)
        ids = []
        with s.db:
            for owner in (11, 22):
                p = s.person(owner, "Андрей")
                loc = s.location(owner, "Baam")
                tid = s.execute("INSERT INTO trainings(owner,date,location_id,court_cost,created_by) VALUES (?,?,?,?,?)", (owner, "2026-09-25", loc, 100, owner)).lastrowid
                s.add_participant(owner, tid, p)
                s.set_court_payment(owner, tid, p, 100)
                s.lock(owner, tid)
                ids.append((p, tid))
        s.initialize_shared()
        self.assertEqual(s.one("SELECT COUNT(*) FROM people WHERE saved=1")[0], 1)
        self.assertEqual(s.one("SELECT COUNT(*) FROM locations")[0], 1)
        for p, tid in ids:
            self.assertEqual(s.participants(tid)[0]["person_id"], p)
            self.assertEqual(s.calculation(0, tid).total, 100)


class MembershipTests(unittest.TestCase):
    def test_membership_statuses_and_bot_privileges(self):
        class API:
            bot_status = "administrator"
            member = {}
            def call(self, method, payload):
                return {"status": self.bot_status} if payload["user_id"] == 123 else self.member
        api = API()
        check = Membership(api, 123)
        for status, is_member, expected in (("member", False, True), ("creator", False, True),
                                          ("administrator", False, True), ("restricted", True, True),
                                          ("restricted", False, False), ("left", False, False), ("kicked", False, False)):
            api.member = {"status": status, "is_member": is_member}
            self.assertEqual(check(-100, 22), expected)
        api.bot_status = "member"
        with self.assertRaises(ValidationError):
            check(-100, 22)
