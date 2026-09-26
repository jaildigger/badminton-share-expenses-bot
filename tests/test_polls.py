import json
import unittest
from datetime import date
from unittest.mock import Mock

from badminton import polls
from badminton.bot import Bot
from badminton.calculator import ValidationError
from badminton.miniapp import Conflict, mutate, snapshot
from badminton.store import Store
from badminton.telegram import TelegramError
from tests import test_bot


class PollParsingTests(unittest.TestCase):
    def test_dates_and_rollover(self):
        self.assertEqual(polls.poll_date('Baam 01/01', date(2026, 12, 30)), '2027-01-01')
        self.assertEqual(polls.poll_date('Grand · 31.12', date(2027, 1, 2)), '2026-12-31')
        for title in ('31/02', '26/09 и 27/09', '19:00', '26/09/2026', '126/09'):
            with self.assertRaises(ValidationError):
                polls.poll_date(title, date(2026, 9, 26))

    def test_normalization_and_validation(self):
        for answer in [' Y e S ', ' Д А ', ' 1 9 : 0 0 - 2 1 : 0 0 ', '\t18:00 - 20:00\n', '19:00']:
            self.assertIn(polls.normalize(answer), polls.YES_OPTIONS)
        self.assertNotIn(polls.normalize('Thinking'), polls.YES_OPTIONS)
        for options in (['Yes'], ['Yes', 'y e s'], ['Yes', ''], ['a'] * 13):
            with self.assertRaises(ValidationError):
                polls.validate('Baam 26/09', options)


class PollTests(unittest.TestCase):
    def setUp(self):
        self.s = Store(':memory:')
        self.addCleanup(self.s.db.close)
        self.s.initialize_shared()
        with self.s.db:
            self.lid = self.s.location(0, 'Baam')
            self.s.execute("INSERT INTO group_bindings VALUES (0,-100123,'Команда',42)")
        self.api = Mock()
        self.api.call.return_value = {'message_id': 100, 'poll': {'id': 'tg-poll', 'options': [
            {'persistent_id': 'time', 'text': '19:00-21:00', 'voter_count': 0},
            {'persistent_id': 'think', 'text': 'Thinking', 'voter_count': 0},
            {'persistent_id': 'no', 'text': 'No', 'voter_count': 0}]}}
        with self.s.db:
            self.pid = polls.queue(self.s, 0, 11, 'Baam 26/09', polls.DEFAULT_OPTIONS)
        polls.deliver(self.s, self.api, self.pid)

    def vote(self, uid, choice, name='Аня'):
        with self.s.db:
            polls.receive(self.s, {'poll_answer': {'poll_id': 'tg-poll', 'user': {'id': uid, 'first_name': name}, 'option_persistent_ids': choice}})

    def count(self, count):
        options = json.loads(self.s.one('SELECT options FROM attendance_polls WHERE id=?', (self.pid,))[0])
        options[0]['voter_count'] = count
        with self.s.db:
            polls.receive(self.s, {'poll': {'id': 'tg-poll', 'options': options, 'is_closed': False}})

    def test_native_flags_and_delivery_idempotence(self):
        method, payload = self.api.call.call_args[0]
        self.assertEqual(method, 'sendPoll')
        for flag in ('allows_revoting', 'allow_adding_options'):
            self.assertTrue(payload[flag])
        self.assertFalse(payload['is_anonymous'])
        self.assertFalse(payload['allows_multiple_answers'])
        self.assertEqual(payload['message_thread_id'], 42)
        polls.deliver(self.s, self.api, self.pid)
        self.assertEqual(self.api.call.call_count, 1)

    def test_changes_withdrawals_added_options_and_reordering(self):
        self.vote(22, ['time'])
        self.count(1)
        self.assertEqual(len(polls.latest(self.s, 0)['participants']), 1)
        self.vote(22, ['think'])
        self.count(0)
        self.assertEqual(polls.latest(self.s, 0)['participants'], [])
        options = [dict(persistent_id='new', text=' Д а ', voter_count=1),
                   dict(persistent_id='think', text='Thinking', voter_count=0),
                   dict(persistent_id='time', text='19:00-21:00', voter_count=0)]
        with self.s.db:
            polls.receive(self.s, {'poll': {'id': 'tg-poll', 'options': options, 'is_closed': False}})
        self.vote(22, ['new'])
        result = polls.latest(self.s, 0)
        self.assertEqual(result['participants'], [{'id': 22, 'name': 'Аня'}])
        self.assertEqual(result['options'][0]['voters'][0]['name'], 'Аня')
        self.vote(22, [])
        self.assertEqual(polls.latest(self.s, 0)['participants'], [])

    def test_import_duplicate_date_and_immutable_roster(self):
        self.vote(22, ['time'])
        self.vote(33, ['no'], 'Борис')
        self.count(1)
        c = polls.latest(self.s, 0)
        with self.s.db:
            tid = polls.create_training(self.s, 0, 11, c['training_date'], self.lid, 0, self.pid)
        self.assertEqual([p['name'] for p in self.s.participants(tid)], ['Аня'])
        self.assertEqual(self.s.participants(tid)[0]['minutes'], 120)
        with self.assertRaises(ValidationError):
            polls.create_training(self.s, 0, 11, c['training_date'], self.lid, 0, self.pid)
        self.vote(22, ['no'])
        self.assertEqual(len(self.s.participants(tid)), 1)
        self.assertEqual(polls.latest(self.s, 0)['existing_training_id'], tid)

    def test_missing_answers_do_not_create_partial_roster(self):
        self.count(1)
        candidate = polls.latest(self.s, 0)
        self.assertTrue(candidate['incomplete'])
        with self.assertRaises(ValidationError):
            polls.create_training(self.s, 0, 11, candidate['training_date'], self.lid, 0, self.pid)
        self.assertEqual(self.s.one('SELECT COUNT(*) FROM trainings')[0], 0)

    def test_answer_before_new_option_update_waits_for_option(self):
        self.vote(22, ['not-yet-received'])
        self.assertTrue(polls.latest(self.s, 0)['incomplete'])

    def test_zero_unknown_counter_does_not_hide_received_votes(self):
        self.vote(22, ['time'])
        candidate = polls.latest(self.s, 0)
        self.assertFalse(candidate['incomplete'])
        self.assertEqual(len(candidate['participants']), 1)

    def test_changed_group_destination_rejected(self):
        with self.assertRaises(Conflict):
            mutate(self.s, 11, {'action': 'poll_create', 'question': 'Baam 28/09',
                               'options': ['Yes', 'No'], 'chat_id': -100123, 'thread_id': 99})
        self.assertEqual(self.s.one('SELECT COUNT(*) FROM attendance_polls')[0], 1)

    def test_identity_homonyms_and_duplicate_choices(self):
        self.vote(22, ['time', 'time'])
        self.vote(33, ['time'])
        self.count(2)
        c = polls.latest(self.s, 0)
        with self.s.db:
            tid = polls.create_training(self.s, 0, 11, c['training_date'], self.lid, 0, self.pid)
        names = [p['name'] for p in self.s.participants(tid)]
        self.assertEqual(len(set(names)), 2)

    def test_latest_only_unknown_polls_ignored(self):
        with self.s.db:
            polls.receive(self.s, {'poll_answer': {'poll_id': 'foreign', 'user': {'id': 9}, 'option_ids': [0]}})
            second = polls.queue(self.s, 0, 11, 'Baam 27/09', ['Yes', 'No'])
        self.assertEqual(polls.latest(self.s, 0)['id'], self.pid)
        self.api.call.return_value = {'message_id': 101, 'poll': {'id': 'second', 'options': [{'text': 'Yes', 'voter_count': 0}, {'text': 'No', 'voter_count': 0}]}}
        polls.deliver(self.s, self.api, second)
        self.assertEqual(polls.latest(self.s, 0)['id'], second)
        self.assertEqual(self.s.one('SELECT COUNT(*) FROM poll_votes')[0], 0)

    def test_uncertain_delivery_not_automatically_duplicated(self):
        with self.s.db:
            second = polls.queue(self.s, 0, 11, 'Baam 27/09', ['Yes', 'No'])
        self.api.call.side_effect = TelegramError(0)
        polls.deliver(self.s, self.api, second)
        polls.deliver(self.s, self.api, second)
        self.assertEqual(self.api.call.call_count, 2)  # initial fixture + one uncertain attempt
        self.assertEqual(self.s.one('SELECT status FROM attendance_polls WHERE id=?', (second,))[0], 'failed')

    def test_rate_limit_retry_and_request_replay(self):
        with self.s.db:
            second = polls.queue(self.s, 0, 11, 'Baam 27/09', ['Yes', 'No'])
        self.api.call.side_effect = TelegramError(429, retry_after=1)
        with self.assertRaises(TelegramError):
            polls.deliver(self.s, self.api, second)
        self.assertEqual(self.s.one('SELECT status FROM attendance_polls WHERE id=?', (second,))[0], 'queued')

    def test_bot_poll_updates_are_persisted_once(self):
        bot = Bot(self.s, {11})
        update = {'update_id': 50, 'poll_answer': {'poll_id': 'tg-poll', 'user': {'id': 22, 'first_name': 'Аня'}, 'option_ids': [0]}}
        bot.process(update)
        bot.process(update)
        self.assertEqual(self.s.one('SELECT COUNT(*) FROM poll_votes')[0], 1)
        self.assertEqual(self.s.setting('offset'), '51')

    def test_api_creates_poll_and_imports(self):
        with self.s.db:
            self.assertEqual(mutate(self.s, 11, {'action': 'poll_create', 'question': 'Baam 28/09', 'options': ['Yes', 'No'], 'chat_id': -100123, 'thread_id': 42}), 0)
        self.vote(22, ['time'])
        self.count(1)
        state = snapshot(self.s, 11, {11})
        self.assertEqual(state['latest_poll']['id'], self.pid)
        with self.s.db:
            tid = mutate(self.s, 11, dict(action='create', date=state['latest_poll']['training_date'], location_id=self.lid, court_cost='0', poll_id=self.pid))
        self.assertEqual(len(self.s.participants(tid)), 1)


class PollConversationTests(unittest.TestCase):
    def test_editor_and_import(self):
        h = test_bot.BotTests()
        h.setUp()
        self.addCleanup(h.tearDown)
        s = h.store
        s.initialize_shared()
        with s.db:
            s.location(0, 'Baam')
            s.execute("INSERT INTO group_bindings VALUES (0,-100123,'Группа',NULL)")
        h.message('/poll')
        h.message('Baam 26/09')
        self.assertIn('19:00-21:00', h.last()['text'])
        h.click('Изменить варианты')
        h.message('18:00-20:00\nThinking\nNo')
        h.click('Опубликовать голосование')
        poll = s.one('SELECT * FROM attendance_polls')
        api = Mock()
        api.call.return_value = {'message_id': 100, 'poll': {'id': 'tg-poll', 'options': [{'text': '18:00-20:00', 'voter_count': 1}, {'text': 'No', 'voter_count': 0}]}}
        polls.deliver(s, api, poll['id'])
        with s.db:
            polls.receive(s, {'poll_answer': {'poll_id': 'tg-poll', 'user': {'id': 22, 'first_name': 'Аня'}, 'option_ids': [0]}})
        h.message('/new')
        self.assertIn('Аня', h.last()['text'])
        h.click('Продолжить')
        h.message('200')
        tid = s.one('SELECT id FROM trainings')[0]
        self.assertEqual(len(s.participants(tid)), 1)
        h.message('/new')
        self.assertIn('уже есть', h.last()['text'])
        h.message('/polls')
        h.click('Baam 26/09')
        self.assertIn('Аня', h.last()['text'])
        # Resetting financial history retains polls but cascades the training link.
        from badminton.reset_history import reset_history
        with s.db:
            s.execute('DELETE FROM outbox')
        reset_history(h.path, confirm=True)
        self.assertEqual(s.one('SELECT COUNT(*) FROM poll_imports')[0], 0)
        self.assertEqual(len(polls.latest(s, 0)['participants']), 1)
        self.assertIsNone(polls.latest(s, 0)['existing_training_id'])
