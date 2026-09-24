import json
import tempfile
import unittest
from pathlib import Path

from badminton.bot import Bot
from badminton.calculator import ValidationError
from badminton.store import Store
from badminton.__main__ import drain_outbox
from badminton.telegram import TelegramError


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "bot.db"
        self.store = Store(self.path)
        self.bot = Bot(self.store, {11, 22})
        self.update_id = 1

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def message(self, text, uid=11, chat_type="private"):
        update = {"update_id": self.update_id, "message": {"from": {"id": uid}, "chat": {"id": uid, "type": chat_type}, "text": text}}
        self.update_id += 1
        self.bot.process(update)
        return update

    def last(self):
        return json.loads(self.store.one("SELECT payload FROM outbox WHERE method='sendMessage' ORDER BY id DESC LIMIT 1")[0])

    def click(self, label, uid=11):
        buttons = self.last().get("reply_markup", {}).get("inline_keyboard", [])
        data = next(b["callback_data"] for row in buttons for b in row if b["text"] == label)
        return self.callback(data, uid)

    def callback(self, data, uid=11):
        update = {"update_id": self.update_id, "callback_query": {"id": str(self.update_id), "from": {"id": uid},
                   "message": {"chat": {"id": uid, "type": "private"}}, "data": data}}
        self.update_id += 1
        self.bot.process(update)
        return update

    def create(self):
        self.message("/new")
        self.message("25.09.2026")
        self.click("Добавить новую локацию")
        self.message("Grand Baam")
        self.message("350")
        return self.store.one("SELECT id FROM trainings ORDER BY id DESC")[0]

    def fixture(self):
        tid = self.create()
        with self.store.db:
            alice = self.store.person(11, "Андрей")
            bob = self.store.person(11, "Рамиля")
            self.store.add_participant(11, tid, alice)
            self.store.add_participant(11, tid, bob)
            self.store.set_court_payment(11, tid, alice, 35000)
        return tid, alice, bob

    def test_complete_conversation_with_split_payers_partial_payments_and_reopen(self):
        tid = self.create()
        self.click("Добавить нового участника")
        self.message("Андрей")
        self.click("Количество воланов")
        self.message("4")
        self.message("95")
        self.assertIn("380.00", self.last()["text"])
        self.click("К участникам")
        self.click("Добавить нового участника")
        self.message("Рамиля")
        self.click("Изменить время")
        self.message("1,5")
        self.click("К участникам")
        self.click("Добавить гостя")
        self.message("Гость Саша")
        self.click("К участникам")
        self.click("К тренировке")
        self.click("Кто оплатил корт")
        self.click("Андрей")
        self.message("150")
        self.click("Кто оплатил корт")
        self.click("Рамиля")
        self.message("200")
        self.click("Рассчитать")
        self.assertIn("730.00", self.last()["text"])
        self.click("Зафиксировать и отмечать оплаты")
        transfers = self.store.transfers(tid)
        first = transfers[0]
        self.click(first["sender_name"] + " → " + first["recipient_name"])
        self.click("Отметить частичную оплату")
        self.message("10")
        self.assertIn("Оплачено: 10.00", self.last()["text"])
        self.click("Отменить последнюю отметку")
        self.assertIn("Оплачено: 0.00", self.last()["text"])
        self.click("Отметить остаток оплаченным")
        self.click("К расчёту")
        for tr in self.store.transfers(tid):
            if tr["paid"] < tr["amount"]:
                self.click(tr["sender_name"] + " → " + tr["recipient_name"])
                self.click("Отметить остаток оплаченным")
                self.click("К расчёту")
        self.click("Закрыть тренировку")
        self.assertEqual(self.store.training(11, tid)["status"], "closed")
        self.click("Открыть снова для исправления оплат")
        self.assertEqual(self.store.training(11, tid)["status"], "settling")
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM people WHERE saved=1")[0], 2)

    def test_restart_mid_prompt(self):
        tid = self.create()
        self.click("Добавить нового участника")
        self.store.db.close()
        self.store = Store(self.path)
        self.bot = Bot(self.store, {11})
        self.message("Маша")
        self.assertEqual(self.store.participants(tid)[0]["name"], "Маша")
        self.assertEqual(self.store.participants(tid)[0]["minutes"], 120)
        self.assertEqual(self.store.participants(tid)[0]["shuttle_count"], 0)

    def test_duplicate_input_and_stale_button_do_not_duplicate_money(self):
        tid, _, _ = self.fixture()
        with self.store.db:
            self.store.lock(11, tid)
            tr = self.store.transfers(tid)[0]
            self.bot.transfer_view(11, tr["id"])
        callback = self.click("Отметить остаток оплаченным")
        self.bot.process(callback)
        self.callback(callback["callback_query"]["data"])
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM payments")[0], 1)
        self.assertIn("старое меню", self.last()["text"])

    def test_invalid_input_preserves_prompt_and_recovers(self):
        tid = self.create()
        self.click("Добавить нового участника")
        self.message("Лена")
        self.click("Изменить время")
        self.message("0")
        self.assertIn("от 1 минуты", self.last()["text"])
        self.message("1,5")
        self.assertEqual(self.store.participants(tid)[0]["minutes"], 90)

    def test_permissions_and_private_chats(self):
        tid, _, _ = self.fixture()
        self.message("/start", 99)
        self.assertIn("Доступ только", self.last()["text"])
        self.message("/start", 22)
        generation, _ = self.store.session(22)
        self.callback("{}|view:{}".format(generation, tid), 22)
        self.assertIn("не найдена", self.last()["text"])
        count = self.store.one("SELECT COUNT(*) FROM outbox")[0]
        self.message("/new", 11, "group")
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM outbox")[0], count)

    def test_saved_locations_and_participants_reusable_but_guests_not(self):
        tid, alice, _ = self.fixture()
        with self.store.db:
            guest = self.store.person(11, "Гость", saved=False)
            self.store.add_participant(11, tid, guest)
        self.message("/new")
        self.message("2026-09-26")
        self.click("Grand Baam")
        self.message("200")
        self.click("Выбрать из сохранённых")
        labels = [b["text"] for row in self.last()["reply_markup"]["inline_keyboard"] for b in row]
        self.assertIn("Андрей", labels)
        self.assertNotIn("Гость", labels)
        self.click("Андрей")
        new_tid = self.store.one("SELECT MAX(id) FROM trainings")[0]
        self.assertEqual(self.store.participants(new_tid)[0]["person_id"], alice)

    def test_cannot_edit_or_close_with_outstanding_payments(self):
        tid, alice, _ = self.fixture()
        with self.store.db:
            self.store.lock(11, tid)
            tr = self.store.transfers(tid)[0]
            self.store.pay(11, tr["id"], 100)
            for operation in (lambda: self.store.update_participant(11, tid, alice, "minutes", 60),
                              lambda: self.store.unlock(11, tid), lambda: self.store.close_training(11, tid),
                              lambda: self.store.pay(11, tr["id"], tr["amount"])):
                with self.assertRaises(ValidationError):
                    operation()
            self.store.undo_payment(11, tr["id"])
            self.store.unlock(11, tid)
            self.assertEqual(self.store.training(11, tid)["status"], "draft")

    def test_insufficient_court_funding_and_missing_shuttle_price(self):
        tid = self.create()
        with self.store.db:
            pid = self.store.person(11, "А")
            self.store.add_participant(11, tid, pid)
            with self.assertRaises(ValidationError):
                self.store.lock(11, tid)
            self.store.set_court_payment(11, tid, pid, 35000)
            self.store.update_participant(11, tid, pid, "shuttle_count", 2)
            with self.assertRaises(ValidationError):
                self.store.lock(11, tid)

    def test_pagination(self):
        tid = self.create()
        with self.store.db:
            for i in range(20):
                self.store.person(11, "Игрок {}".format(i))
        self.click("Выбрать из сохранённых")
        self.click("Следующие")
        self.click("Игрок 8")
        self.assertEqual(self.store.participants(tid)[0]["name"], "Игрок 8")

    def test_outbox_retry_does_not_repeat_financial_action(self):
        tid, _, _ = self.fixture()
        with self.store.db:
            self.store.lock(11, tid)
            tr = self.store.transfers(tid)[0]
            self.bot.transfer_view(11, tr["id"])
        self.click("Отметить остаток оплаченным")
        count = self.store.one("SELECT COUNT(*) FROM outbox")[0]

        class Offline:
            def call(self, *_):
                raise TelegramError(0)

        with self.assertRaises(TelegramError):
            drain_outbox(self.store, Offline())
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM outbox")[0], count)

        class Online:
            def call(self, *_):
                return True

        drain_outbox(self.store, Online())
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM outbox")[0], 0)
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM payments")[0], 1)

    def test_transaction_rolls_back_on_unexpected_failure(self):
        tid = self.create()
        self.click("Добавить нового участника")
        original = self.bot.person_view

        def broken(*args):
            raise RuntimeError("simulated failure")

        self.bot.person_view = broken
        offset = self.store.setting("offset")
        with self.assertRaises(RuntimeError):
            self.message("Новый")
        self.assertEqual(self.store.setting("offset"), offset)
        self.assertEqual(self.store.participants(tid), [])
        self.assertIsNone(self.store.one("SELECT * FROM people WHERE name='Новый'"))
        self.bot.person_view = original


if __name__ == "__main__":
    unittest.main()
