"""Disposable server for browser tests. Never reads .env or the real database."""
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from badminton.miniapp import make_server
from badminton.store import Store
from badminton import polls
from tests.test_miniapp import TOKEN, signed

with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / 'browser.sqlite3'
    store = Store(path)
    store.initialize_shared()
    store.seed_defaults({0})
    with store.db:
        store.execute("INSERT INTO group_bindings VALUES (0,-100,'Snowflakes · Бадминтон',NULL)")
        if os.environ.get('MINIAPP_TEST_POLL') == '1':
            pid = polls.queue(store, 0, 11, 'Baam · ' + datetime.now(polls.TZ).strftime('%d/%m'), polls.DEFAULT_OPTIONS)
            options = [{'text': '19:00-21:00', 'persistent_id': 'yes', 'voter_count': 1},
                       {'text': 'Thinking', 'persistent_id': 'maybe', 'voter_count': 0},
                       {'text': 'No', 'persistent_id': 'no', 'voter_count': 0}]
            store.execute("UPDATE attendance_polls SET status='sent',telegram_id='fixture',message_id=123,options=? WHERE id=?", (json.dumps(options), pid))
            store.execute('DELETE FROM outbox')
            polls.receive(store, {'poll_answer': {'poll_id': 'fixture', 'user': {'id': 22, 'first_name': 'Настя'}, 'option_persistent_ids': ['yes']}})
    store.db.close()
    server = make_server(path, TOKEN, {11}, None, port=0)
    print(json.dumps({'url': 'http://127.0.0.1:{}'.format(server.server_port), 'auth': signed()}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
