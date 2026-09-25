import hashlib
import hmac
import json
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from badminton.bot import Bot
from badminton.calculator import ValidationError
from badminton.miniapp import (AuthenticationError, Conflict, dispatch, initialize_api,
                               make_server, snapshot, validate_init_data)
from badminton.store import Store

TOKEN = '123456:test-only-token'


def signed(uid=11, now=None):
    fields = {'auth_date': str(int(time.time()) if now is None else now), 'user': json.dumps({'id': uid, 'first_name': 'Test'})}
    secret = hmac.new(b'WebAppData', TOKEN.encode(), hashlib.sha256).digest()
    check = '\n'.join('{}={}'.format(k, v) for k, v in sorted(fields.items()))
    fields['hash'] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class AuthenticationTests(unittest.TestCase):
    def test_signed_identity_and_age(self):
        self.assertEqual(validate_init_data(signed(now=1000), TOKEN, now=1001), 11)
        for data in ('', signed(now=1000), signed(now=1000000), signed(now=90000)+'&user=other'):
            with self.assertRaises(AuthenticationError):
                validate_init_data(data, TOKEN, now=90001)

    def test_tampered_data_and_wrong_token(self):
        with self.assertRaises(AuthenticationError):
            validate_init_data(signed().replace('Test', 'Fake'), TOKEN)
        with self.assertRaises(AuthenticationError):
            validate_init_data(signed(), 'different')


class MiniAppTests(unittest.TestCase):
    def setUp(self):
        self.s = Store(':memory:')
        self.addCleanup(self.s.db.close)
        self.s.initialize_shared()
        initialize_api(self.s)
        with self.s.db:
            self.lid = self.s.location(0, 'Baam')
            self.alice = self.s.person(0, 'Аня')
            self.bob = self.s.person(0, 'Борис')
        self.tid = 0

    def payload(self, action, **values):
        return dict(action=action, tid=self.tid, revision=self.s.revision(), request_id=str(uuid.uuid4()), **values)

    def run_action(self, action, **values):
        result = dispatch(self.s, 11, {11}, self.payload(action, **values))
        if 'training' in result:
            self.tid = result['training']['id']
        return result

    def fixture(self):
        self.run_action('create', date='2026-09-25', location_id=self.lid, court_cost='200')
        self.run_action('add', pid=self.alice)
        self.run_action('add', pid=self.bob)
        self.run_action('payer', pid=self.alice, amount='200')

    def test_complete_lifecycle_and_same_bot_data(self):
        self.fixture()
        view = self.run_action('participant', pid=self.bob, hours='1,5', shuttle_count='2', shuttle_price='30')
        self.assertEqual(view['training']['calculation']['total'], 26000)
        self.assertEqual(self.s.participants(self.tid)[1]['minutes'], 90)
        self.run_action('lock')
        tr = self.s.transfers(self.tid)[0]
        self.run_action('pay', transfer_id=tr['id'], amount='10')
        with self.assertRaises(ValidationError):
            self.run_action('close')
        with self.assertRaises(ValidationError):
            self.run_action('unlock')
        self.run_action('undo', transfer_id=tr['id'])
        self.run_action('pay', transfer_id=tr['id'], full=True)
        self.run_action('close')
        self.assertEqual(self.s.training(0, self.tid)['status'], 'closed')
        self.run_action('reopen')
        self.run_action('undo', transfer_id=tr['id'])
        self.run_action('unlock')
        self.run_action('details', date='26.09.2026', location_name='Grand', court_cost='300')
        self.assertEqual(self.s.training(0, self.tid)['location'], 'Grand')
        self.assertEqual(Bot(self.s, {11}).scope(11), 0)

    def test_guest_and_nonplaying_payer(self):
        self.fixture()
        self.run_action('add', name='Гость', saved=False)
        guest = self.s.participants(self.tid)[-1]
        self.assertEqual(guest['saved'], 0)
        self.run_action('remove', pid=guest['person_id'])
        self.run_action('payer', pid=self.alice, amount='0')
        self.run_action('payer', name='Не играет', amount='200')
        view = snapshot(self.s, 11, {11}, self.tid)
        self.assertEqual(len(view['training']['participants']), 2)
        self.assertEqual(len(view['training']['calculation']['transfers']), 2)

    def test_stale_revision_and_replay(self):
        self.fixture()
        self.run_action('lock')
        tr = self.s.transfers(self.tid)[0]
        body = self.payload('pay', transfer_id=tr['id'], amount='10')
        dispatch(self.s, 11, {11}, body)
        dispatch(self.s, 11, {11}, body)
        self.assertEqual(self.s.transfers(self.tid)[0]['paid'], 1000)
        with self.assertRaises(Conflict):
            dispatch(self.s, 11, {11}, dict(body, request_id=str(uuid.uuid4())))
        with self.assertRaises(Conflict):
            dispatch(self.s, 11, {11}, dict(body, amount='20'))
        # The same stale revision cannot pay again under another user's identity.
        with self.assertRaises(Conflict):
            dispatch(self.s, 22, {11}, body)

    def test_create_replay_does_not_duplicate(self):
        body = self.payload('create', date='2026-09-25', location_id=self.lid, court_cost='0')
        first = dispatch(self.s, 11, {11}, body)
        second = dispatch(self.s, 11, {11}, body)
        self.assertEqual(first['training']['id'], second['training']['id'])
        self.assertEqual(len(second['trainings']), 1)

    def test_atomic_validation_rollback(self):
        self.fixture()
        before = self.s.revision()
        with self.assertRaises(ValidationError):
            self.run_action('participant', pid=self.bob, hours='1', shuttle_count='2', shuttle_price='NaN')
        self.assertEqual(self.s.participants(self.tid)[1]['minutes'], 120)
        self.assertEqual(before, self.s.revision())
        with self.assertRaises(ValidationError):
            self.run_action('payer', name='Не сохранять', amount='1000')
        self.assertIsNone(self.s.one("SELECT id FROM people WHERE name='Не сохранять'"))
        with self.assertRaises(ValidationError):
            self.run_action('details', date='2026-09-25', location_name='Не сохранять', court_cost='1')
        self.assertIsNone(self.s.one("SELECT id FROM locations WHERE name='Не сохранять'"))

    def test_missing_shuttle_price_and_locked_edit(self):
        self.fixture()
        result = self.run_action('participant', pid=self.bob, hours='2', shuttle_count='1', shuttle_price='0')
        self.assertIn('calculation_error', result['training'])
        with self.assertRaises(ValidationError):
            self.run_action('lock')
        self.run_action('participant', pid=self.bob, hours='2', shuttle_count='0', shuttle_price='0')
        self.run_action('lock')
        with self.assertRaises(ValidationError):
            self.run_action('remove', pid=self.bob)

    def test_publication_queues_and_updates_without_sending(self):
        self.fixture()
        with self.s.db:
            self.s.execute("INSERT INTO group_bindings VALUES (0,-100,'Команда',42)")
        self.run_action('lock')
        with self.assertRaises(Conflict):
            self.run_action('publish', chat_id=-101, thread_id=42)
        self.run_action('publish', chat_id=-100, thread_id=42)
        pub = self.s.one('SELECT * FROM publications')
        self.assertEqual((pub['chat_id'], pub['thread_id'], pub['notify_uid']), (-100, 42, 11))
        before = pub['desired']
        tr = self.s.transfers(self.tid)[0]
        self.run_action('pay', transfer_id=tr['id'], full=True)
        self.assertNotEqual(before, self.s.one('SELECT desired FROM publications')[0])
        self.assertEqual(self.s.one("SELECT COUNT(*) FROM outbox WHERE method='publishTraining'")[0], 2)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'test.sqlite3'
        store = Store(self.path)
        store.initialize_shared()
        with store.db:
            store.execute("INSERT INTO group_bindings VALUES (0,-100,'Команда',NULL)")
        store.db.close()
        self.members = {22}
        self.server = make_server(self.path, TOKEN, {11}, lambda chat, uid: uid in self.members, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = 'http://127.0.0.1:{}'.format(self.server.server_port)

    def get(self, path, auth=None):
        headers = {'X-Telegram-Init-Data': auth} if auth else {}
        return urlopen(Request(self.url+path, headers=headers))

    def test_access_rechecked_for_each_request(self):
        for auth, code in ((None, 401), (signed(99), 403)):
            with self.assertRaises(HTTPError) as error:
                self.get('/api/state', auth)
            self.assertEqual(error.exception.code, code)
        with self.get('/api/state', signed(22)) as response:
            self.assertEqual(response.status, 200)
        self.members.remove(22)
        with self.assertRaises(HTTPError) as error:
            self.get('/api/state', signed(22))
        self.assertEqual(error.exception.code, 403)
        with self.get('/api/state', signed()) as response:
            self.assertTrue(json.load(response)['is_admin'])

    def test_static_allowlist_and_headers(self):
        with self.get('/') as response:
            self.assertIn('text/html', response.headers['Content-Type'])
            self.assertIn("default-src 'self'", response.headers['Content-Security-Policy'])
        with self.assertRaises(HTTPError) as error:
            self.get('/../.env')
        self.assertEqual(error.exception.code, 404)

    def test_post_and_invalid_json(self):
        with self.get('/api/state', signed()) as response:
            revision = json.load(response)['revision']
        body = dict(action='person', name='Тест', revision=revision, request_id=str(uuid.uuid4()))
        headers = {'X-Telegram-Init-Data': signed(), 'Content-Type': 'application/json'}
        with urlopen(Request(self.url+'/api/action', json.dumps(body).encode(), headers)) as response:
            self.assertEqual(json.load(response)['people'][0]['name'], 'Тест')
        for data in (b'[]', b'{invalid'):
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(self.url+'/api/action', data, headers))
            self.assertEqual(error.exception.code, 400)


if __name__ == '__main__':
    unittest.main()
