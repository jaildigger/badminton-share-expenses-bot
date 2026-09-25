import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from badminton.reset_history import reset_history
from badminton.store import Store
from badminton.miniapp import initialize_api


class ResetHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'history.sqlite3'
        self.s = Store(self.path)
        self.addCleanup(self.s.db.close)
        self.s.initialize_shared()
        initialize_api(self.s)
        with self.s.db:
            alice = self.s.person(0, 'Аня')
            bob = self.s.person(0, 'Борис')
            location = self.s.location(0, 'Baam')
            self.tid = self.s.execute("INSERT INTO trainings(owner,date,location_id,court_cost) VALUES (0,'2026-09-25',?,20000)", (location,)).lastrowid
            self.s.add_participant(0, self.tid, alice)
            self.s.add_participant(0, self.tid, bob)
            self.s.set_court_payment(0, self.tid, alice, 20000)
            self.s.lock(0, self.tid)
            self.s.pay(0, self.s.transfers(self.tid)[0]['id'], 5000)
            self.s.execute("INSERT INTO group_bindings VALUES (0,-100,'Группа',NULL)")
            self.s.execute("INSERT INTO publications(training_id,chat_id,title) VALUES (?,-100,'Группа')", (self.tid,))
            self.s.execute("INSERT INTO publication_messages VALUES (?,0,99,'Итог')", (self.tid,))
            self.s.execute("INSERT INTO sessions VALUES (11,50,?)", (json.dumps({'tid': self.tid}),))
            self.s.execute("INSERT INTO miniapp_requests VALUES (11,'request-id','hash',?,1)", (self.tid,))
            self.s.set_setting('offset', 12345)

    def test_dry_run_does_not_change_anything(self):
        revision = self.s.revision()
        result = reset_history(self.path)
        self.assertTrue(result['dry_run'])
        self.assertEqual(result['counts']['trainings'], 1)
        self.assertEqual(self.s.revision(), revision)
        self.assertEqual(len(list(self.path.parent.glob('*.before-reset-*'))), 0)

    def test_reset_preserves_directories_group_offset_and_valid_backup(self):
        before = self.s.revision()
        result = reset_history(self.path, confirm=True)
        self.assertEqual(result['remaining_trainings'], 0)
        for table in ('trainings', 'payments', 'transfers', 'participants', 'court_payments', 'publications', 'publication_messages', 'miniapp_requests'):
            self.assertEqual(self.s.one('SELECT COUNT(*) FROM '+table)[0], 0)
        for table, count in (('people', 2), ('locations', 1), ('group_bindings', 1)):
            self.assertEqual(self.s.one('SELECT COUNT(*) FROM '+table)[0], count)
        self.assertEqual(self.s.setting('offset'), '12345')
        self.assertGreater(int(self.s.revision()), int(before))
        session = self.s.one('SELECT * FROM sessions WHERE owner=11')
        self.assertEqual((session['generation'], session['state']), (51, '{}'))
        with sqlite3.connect(result['backup']) as backup:
            self.assertEqual(backup.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(backup.execute('SELECT COUNT(*) FROM trainings').fetchone()[0], 1)
            self.assertEqual(backup.execute('SELECT SUM(amount) FROM payments').fetchone()[0], 5000)
        self.assertEqual(Path(result['backup']).stat().st_mode & 0o777, 0o600)

    def test_pending_outbox_refuses_without_partial_deletion(self):
        with self.s.db:
            self.s.enqueue('publishTraining', {'training_id': self.tid})
        with self.assertRaisesRegex(ValueError, 'очереди'):
            reset_history(self.path, confirm=True)
        self.assertEqual(self.s.one('SELECT COUNT(*) FROM trainings')[0], 1)
        self.assertEqual(self.s.one('SELECT COUNT(*) FROM outbox')[0], 1)
        self.assertEqual(len(list(self.path.parent.glob('*.before-reset-*'))), 0)

    def test_missing_database_not_created(self):
        wrong = self.path.with_name('typo.sqlite3')
        with self.assertRaises(sqlite3.OperationalError):
            reset_history(wrong, confirm=True)
        self.assertFalse(wrong.exists())


if __name__ == '__main__':
    unittest.main()
