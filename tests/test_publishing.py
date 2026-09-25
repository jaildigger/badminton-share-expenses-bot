import json
import unittest

from badminton import publishing
from badminton.__main__ import drain_outbox
from badminton.bot import Bot
from badminton.store import Store
from badminton.telegram import TelegramError
from tests import test_bot


class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.next_id = 100
        self.fail = None

    def call(self, method, payload):
        if self.fail and payload.get("chat_id", 0) < 0:
            raise self.fail
        self.calls.append((method, payload))
        self.next_id += 1
        return {"message_id": self.next_id}

    def group_calls(self):
        return [(m, p) for m, p in self.calls if p.get("chat_id", 0) < 0]


class PublishingTests(unittest.TestCase):
    def setUp(self):
        self.h = test_bot.BotTests()
        self.h.setUp()
        self.addCleanup(self.h.tearDown)
        self.s = self.h.store
        self.api = FakeTelegram()
        self.h.last = self.current_menu

    def current_menu(self):
        # A real Telegram client retains delivered keyboards after the outbox drains.
        candidates = [p for method, p in self.api.calls if method == "sendMessage"]
        candidates += [json.loads(row[0]) for row in self.s.all(
            "SELECT payload FROM outbox WHERE method='sendMessage' ORDER BY id")]
        generation, _ = self.s.session(11)
        for payload in reversed(candidates):
            keyboard = payload.get("reply_markup", {}).get("inline_keyboard", [])
            if payload.get("chat_id") == 11 and keyboard and keyboard[0][0]["callback_data"].startswith(str(generation) + "|"):
                return payload
        raise AssertionError("No current keyboard in queued or delivered messages")

    def bind(self, uid=11, chat_id=-100, command="/bind@badminton_share_bot", thread_id=None):
        update = {"update_id": self.h.update_id, "message": {"from": {"id": uid},
                  "chat": {"id": chat_id, "type": "supergroup", "title": "Бадминтон"}, "text": command}}
        if thread_id:
            update["message"]["message_thread_id"] = thread_id
        self.h.update_id += 1
        self.h.bot.process(update)

    def publish(self):
        tid, _, _ = self.h.fixture()
        self.bind(thread_id=7)
        with self.s.db:
            self.s.lock(11, tid)
            self.h.bot.settlements(11, tid)
        self.h.click("Опубликовать итог в группе")
        self.assertEqual(self.s.one("SELECT COUNT(*) FROM publications")[0], 0)
        self.h.click("Опубликовать в «Бадминтон»")
        drain_outbox(self.s, self.api)
        return tid

    def test_bind_only_allowlisted_organizer_and_correct_bot(self):
        self.bind(uid=99)
        self.bind(command="/bind@different_bot")
        self.assertEqual(self.s.one("SELECT COUNT(*) FROM group_bindings")[0], 0)
        self.bind()
        self.assertEqual(self.s.one("SELECT chat_id FROM group_bindings WHERE owner=11")[0], -100)
        self.assertEqual(self.s.one("SELECT COUNT(*) FROM publications")[0], 0)

    def test_publish_then_partial_payment_edits_same_message(self):
        tid = self.publish()
        sent = self.api.group_calls()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "sendMessage")
        self.assertEqual(sent[0][1]["message_thread_id"], 7)
        self.assertNotIn("reply_markup", sent[0][1])
        self.assertIn("Рамиля → Андрей: 175.00", sent[0][1]["text"])
        message_id = self.s.one("SELECT message_id FROM publication_messages")[0]
        self.h.click("Рамиля → Андрей")
        self.h.click("Отметить частичную оплату")
        self.h.message("50")
        drain_outbox(self.s, self.api)
        method, payload = self.api.group_calls()[-1]
        self.assertEqual(method, "editMessageText")
        self.assertEqual(payload["message_id"], message_id)
        self.assertIn("осталось 125.00", payload["text"])
        self.h.click("Отменить последнюю отметку")
        drain_outbox(self.s, self.api)
        self.assertIn("осталось 175.00", self.api.group_calls()[-1][1]["text"])

    def test_unpublished_training_and_navigation_send_no_group_data(self):
        tid, _, _ = self.h.fixture()
        self.bind()
        with self.s.db:
            self.s.lock(11, tid)
        self.h.message("/start")
        drain_outbox(self.s, self.api)
        self.assertEqual(self.api.group_calls(), [])

    def test_restart_reuses_message_and_manual_refresh_does_not_duplicate(self):
        tid = self.publish()
        self.s.db.close()
        self.h.store = self.s = Store(self.h.path)
        self.h.bot = Bot(self.s, {11})
        self.h.click("Обновить итог в группе")
        drain_outbox(self.s, self.api)
        self.assertEqual(len(self.api.group_calls()), 1)
        self.assertEqual(self.s.one("SELECT COUNT(*) FROM publication_messages")[0], 1)

    def test_unlock_marks_old_amounts_invalid_then_relock_updates(self):
        tid = self.publish()
        self.h.click("Вернуть к редактированию")
        drain_outbox(self.s, self.api)
        self.assertIn("Прежние суммы неактуальны", self.api.group_calls()[-1][1]["text"])
        self.h.click("Рассчитать")
        self.h.click("Зафиксировать и отмечать оплаты")
        drain_outbox(self.s, self.api)
        self.assertIn("Рамиля → Андрей", self.api.group_calls()[-1][1]["text"])
        self.h.click("Рамиля → Андрей")
        self.h.click("Отметить остаток оплаченным")
        self.h.click("К расчёту")
        self.h.click("Закрыть тренировку")
        drain_outbox(self.s, self.api)
        self.assertIn("Тренировка закрыта", self.api.group_calls()[-1][1]["text"])

    def test_forbidden_group_notifies_owner_and_does_not_block_private_outbox(self):
        tid = self.publish()
        self.api.fail = TelegramError(403)
        self.h.click("Рамиля → Андрей")
        self.h.click("Отметить остаток оплаченным")
        drain_outbox(self.s, self.api)
        self.assertEqual(self.s.one("SELECT COUNT(*) FROM outbox")[0], 0)
        self.assertTrue(any("Не удалось" in p.get("text", "") and p["chat_id"] == 11 for _, p in self.api.calls))
        self.assertEqual(self.s.transfers(tid)[0]["paid"], 17500)
        self.api.fail = None
        self.h.click("К расчёту")
        self.h.click("Обновить итог в группе")
        drain_outbox(self.s, self.api)
        self.assertIn("оплачено", self.api.group_calls()[-1][1]["text"])

    def test_long_report_chunks_and_replaced_extra_parts(self):
        tid = self.publish()
        text = "\n".join("😀"*60 + " {}".format(i) for i in range(100))
        with self.s.db:
            self.s.execute("UPDATE publications SET desired=? WHERE training_id=?", (text, tid))
        publishing.deliver(self.s, self.api, tid)
        self.assertGreater(self.s.one("SELECT COUNT(*) FROM publication_messages")[0], 1)
        for _, payload in self.api.group_calls():
            self.assertLess(len(payload["text"].encode("utf-16-le")) // 2, 4096)
        with self.s.db:
            self.s.execute("UPDATE publications SET desired='Короткий итог' WHERE training_id=?", (tid,))
        publishing.deliver(self.s, self.api, tid)
        for row in self.s.all("SELECT part,text FROM publication_messages"):
            self.assertEqual(row["text"], "Короткий итог" if row["part"] == 0 else "Расчёт #{} обновлён. Актуальные данные — в первой части сообщения.".format(tid))

    def test_rebinding_does_not_move_existing_publication(self):
        tid = self.publish()
        self.bind(chat_id=-200)
        self.h.click("Рамиля → Андрей")
        self.h.click("Отметить остаток оплаченным")
        drain_outbox(self.s, self.api)
        self.assertTrue(all(p["chat_id"] == -100 for _, p in self.api.group_calls()))

    def test_deleted_group_message_is_replaced_on_update(self):
        tid = self.publish()
        old_id = self.s.one("SELECT message_id FROM publication_messages")[0]
        original = self.api.call

        def missing_message(method, payload):
            if method == "editMessageText":
                raise TelegramError(400, reason="message_missing")
            return original(method, payload)

        self.api.call = missing_message
        self.h.click("Рамиля → Андрей")
        self.h.click("Отметить остаток оплаченным")
        drain_outbox(self.s, self.api)
        self.assertNotEqual(self.s.one("SELECT message_id FROM publication_messages")[0], old_id)
        self.assertEqual(self.api.group_calls()[-1][0], "sendMessage")
        self.assertIn("оплачено", self.api.group_calls()[-1][1]["text"])

    def test_network_retry_preserves_publication_and_financial_state(self):
        tid = self.publish()
        self.api.fail = TelegramError(0)
        self.h.click("Рамиля → Андрей")
        self.h.click("Отметить остаток оплаченным")
        with self.assertRaises(TelegramError):
            drain_outbox(self.s, self.api)
        self.assertEqual(self.s.transfers(tid)[0]["paid"], 17500)
        self.assertGreater(self.s.one("SELECT COUNT(*) FROM outbox")[0], 0)
        self.api.fail = None
        drain_outbox(self.s, self.api)
        self.assertEqual(self.s.one("SELECT COUNT(*) FROM payments")[0], 1)
        self.assertEqual(self.s.one("SELECT COUNT(*) FROM publication_messages")[0], 1)
        self.assertIn("оплачено", self.api.group_calls()[-1][1]["text"])
