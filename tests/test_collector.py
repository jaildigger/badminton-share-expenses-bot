import unittest

from badminton.bot import Bot
from badminton.calculator import Transfer, ValidationError
from badminton.miniapp import snapshot
from badminton.publishing import summary
from badminton.store import Store


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.s = Store(':memory:')
        self.addCleanup(self.s.db.close)
        self.s.initialize_shared()
        with self.s.db:
            self.a = self.s.person(0, 'Андрей')
            self.m = self.s.person(0, 'Мария')
            self.b = self.s.person(0, 'Борис')
            lid = self.s.location(0, 'Baam')
            self.tid = self.s.execute("INSERT INTO trainings(owner,date,location_id,court_cost) VALUES (0,'2026-11-04',?,30000)", (lid,)).lastrowid
            for pid in (self.a, self.m, self.b):
                self.s.add_participant(0, self.tid, pid)
            self.s.update_participant(0, self.tid, self.m, 'shuttle_count', 6)
            self.s.update_participant(0, self.tid, self.m, 'shuttle_price', 10000)
            self.s.set_court_payment(0, self.tid, self.a, 30000)

    def test_refund_in_preview_api_chat_and_publication(self):
        note = self.s.settlement_notes(0, self.tid)[0]
        self.assertIn('Плательщик корта «Андрей» должен вернуть участнику «Мария» 300.00', note)
        state = snapshot(self.s, 11, {11}, self.tid)
        self.assertEqual(state['training']['calculation']['balances'][self.m], -30000)
        self.assertEqual(state['training']['settlement_notes'], [note])
        lines, _, _ = Bot(self.s, {11}).report(11, self.tid)
        self.assertIn(note, lines)
        with self.s.db:
            self.s.lock(0, self.tid)
        self.assertIn(note, summary(self.s, 0, self.tid))
        refund = next(tr for tr in self.s.transfers(self.tid) if tr['recipient'] == self.m)
        with self.s.db:
            self.s.pay(0, refund['id'], 10000)
        self.assertIn('200.00', self.s.settlement_notes(0, self.tid)[0])
        with self.s.db:
            self.s.pay(0, refund['id'], 20000)
        self.assertEqual(self.s.settlement_notes(0, self.tid), [])
        with self.s.db:
            self.s.undo_payment(0, refund['id'])
        self.assertIn('200.00', self.s.settlement_notes(0, self.tid)[0])

    def test_historical_routes_and_partial_payments_remain_unchanged(self):
        with self.s.db:
            self.s.execute("UPDATE trainings SET status='settling' WHERE id=?", (self.tid,))
            transfer_id = self.s.execute('INSERT INTO transfers(training_id,sender,recipient,amount) VALUES (?,?,?,30000)', (self.tid, self.b, self.m)).lastrowid
            self.s.pay(0, transfer_id, 12000)
        old = [Transfer(self.b, self.m, 30000)]
        self.assertEqual(self.s.calculation(0, self.tid).transfers, old)
        self.assertEqual(self.s.transfers(self.tid)[0]['paid'], 12000)
        self.assertEqual(self.s.settlement_notes(0, self.tid), [])
        with self.s.db:
            self.s.pay(0, transfer_id, 18000)
            self.s.close_training(0, self.tid)
        self.assertEqual(self.s.calculation(0, self.tid).transfers, old)


    def test_single_payer_replacement_and_cost_changes(self):
        with self.s.db:
            self.s.set_court_payment(0, self.tid, self.b, 30000)
        rows = self.s.all('SELECT * FROM court_payments WHERE training_id=?', (self.tid,))
        self.assertEqual([(r['person_id'], r['amount']) for r in rows], [(self.b, 30000)])
        with self.assertRaises(ValidationError):
            with self.s.db:
                self.s.set_court_payment(0, self.tid, self.a, 10000)
        with self.s.db:
            self.s.set_court_cost(0, self.tid, 10000)
        self.assertEqual(self.s.one('SELECT amount FROM court_payments WHERE training_id=?', (self.tid,))[0], 10000)
        self.s.calculation(0, self.tid)

    def test_legacy_split_draft_requires_explicit_payer_choice(self):
        with self.s.db:
            self.s.execute('UPDATE court_payments SET amount=10000 WHERE training_id=?', (self.tid,))
            self.s.execute('INSERT INTO court_payments VALUES (?,?,20000)', (self.tid, self.b))
        with self.assertRaisesRegex(ValidationError, 'одного плательщика'):
            self.s.lock(0, self.tid)
        with self.s.db:
            self.s.set_court_payment(0, self.tid, self.a, 30000)
            self.s.lock(0, self.tid)
