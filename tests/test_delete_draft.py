import json
import unittest

from badminton import polls
from badminton.bot import Bot
from badminton.calculator import ValidationError
from badminton.miniapp import Conflict, dispatch
from tests import test_bot, test_miniapp


class DeleteDraftTests(unittest.TestCase):
    def setUp(self):
        self.h = test_miniapp.MiniAppTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.h.fixture()
        self.s = self.h.s
        self.tid = self.h.tid

    def test_delete_cleanup_and_retry_preserves_directories_and_poll(self):
        with self.s.db:
            self.s.execute("INSERT INTO group_bindings VALUES (0,-100,'Группа',NULL)")
            pid = polls.queue(self.s, 0, 11, 'Baam 03/11', ['Yes', 'No'])
            self.s.execute('INSERT INTO poll_imports VALUES (?,?)', (pid, self.tid))
            self.s.execute("INSERT INTO sessions VALUES (11,10,?)", (json.dumps({'tid': self.tid}),))
        body = self.h.payload('delete_draft')
        first = dispatch(self.s, 11, {11}, body)
        second = dispatch(self.s, 11, {11}, body)
        self.assertEqual(first, second)
        self.assertNotIn('training', first)
        self.assertEqual(first['trainings'], [])
        for table in ('trainings', 'participants', 'court_payments', 'poll_imports'):
            self.assertEqual(self.s.one('SELECT COUNT(*) FROM '+table)[0], 0)
        self.assertEqual(self.s.one('SELECT COUNT(*) FROM people')[0], 2)
        self.assertEqual(self.s.one('SELECT COUNT(*) FROM locations')[0], 1)
        self.assertEqual(self.s.one('SELECT COUNT(*) FROM attendance_polls')[0], 1)
        self.assertEqual(tuple(self.s.one('SELECT generation,state FROM sessions WHERE owner=11')), (11, '{}'))
        self.assertEqual(self.s.all('PRAGMA foreign_key_check'), [])

    def test_locked_closed_and_other_owner_rejected(self):
        with self.assertRaises(ValidationError):
            with self.s.db:
                self.s.delete_draft(99, self.tid)
        self.h.run_action('lock')
        with self.assertRaises(ValidationError):
            self.h.run_action('delete_draft')
        for tr in self.s.transfers(self.tid):
            self.h.run_action('pay', transfer_id=tr['id'], full=True)
        self.h.run_action('close')
        with self.assertRaises(ValidationError):
            self.h.run_action('delete_draft')
        self.assertEqual(len(self.s.participants(self.tid)), 2)

    def test_stale_revision_cannot_delete_edited_draft(self):
        body = self.h.payload('delete_draft')
        self.h.run_action('participant', pid=self.h.bob, hours='1', shuttle_count='0', shuttle_price='0')
        with self.assertRaises(Conflict):
            dispatch(self.s, 11, {11}, body)
        self.assertEqual(self.s.training(0, self.tid)['status'], 'draft')

    def test_ids_not_reused_and_old_creation_replay_returns_home(self):
        creation = self.s.one("SELECT * FROM miniapp_requests ORDER BY created_at, rowid LIMIT 1")
        self.h.run_action('delete_draft')
        self.assertEqual(self.s.one('SELECT tid FROM miniapp_requests WHERE request_id=?', (creation['request_id'],))[0], 0)
        self.h.tid = 0
        new = self.h.run_action('create', date='2026-11-03', location_id=self.h.lid, court_cost='0')['training']['id']
        self.assertGreater(new, self.tid)
        with self.assertRaises(ValidationError):
            self.s.training(0, self.tid)

    def test_previously_published_draft_and_pending_delivery(self):
        self.h.run_action('lock')
        with self.s.db:
            self.s.execute("INSERT INTO publications(training_id,chat_id,title) VALUES (?,-100,'Группа')", (self.tid,))
            self.s.execute("INSERT INTO publication_messages VALUES (?,0,99,'Прежний расчёт')", (self.tid,))
        self.h.run_action('unlock')
        with self.assertRaises(ValidationError):
            self.h.run_action('delete_draft')
        self.assertIsNotNone(self.s.one('SELECT 1 FROM trainings WHERE id=?', (self.tid,)))
        with self.s.db:
            self.s.execute("DELETE FROM outbox WHERE method='publishTraining'")
        self.h.run_action('delete_draft')
        self.assertEqual(self.s.one('SELECT COUNT(*) FROM publications')[0], 0)
        self.assertEqual(self.s.one('SELECT COUNT(*) FROM publication_messages')[0], 0)


class DeleteConversationTests(unittest.TestCase):
    def test_confirm_cancel_and_old_buttons(self):
        h = test_bot.BotTests()
        h.setUp()
        self.addCleanup(h.tearDown)
        tid, _, _ = h.fixture()
        with h.store.db:
            h.bot.view(11, tid)
        h.click('Удалить черновик')
        h.click('Отмена')
        self.assertEqual(h.store.training(11, tid)['status'], 'draft')
        h.click('Удалить черновик')
        old = next(b['callback_data'] for row in h.last()['reply_markup']['inline_keyboard'] for b in row if b['text']=='Да, удалить черновик')
        h.click('Да, удалить черновик')
        self.assertIn('Пока нет тренировок', h.last()['text'])
        new_tid = h.create()
        h.callback(old)
        self.assertIn('старое меню', h.last()['text'])
        self.assertGreater(new_tid, tid)
        self.assertEqual(h.store.training(11, new_tid)['status'], 'draft')
