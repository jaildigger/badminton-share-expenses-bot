"""Disposable server for browser tests. Never reads .env or the real database."""
import json
import tempfile
from pathlib import Path

from badminton.miniapp import make_server
from badminton.store import Store
from tests.test_miniapp import TOKEN, signed

with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / 'browser.sqlite3'
    store = Store(path)
    store.initialize_shared()
    store.seed_defaults({0})
    with store.db:
        store.execute("INSERT INTO group_bindings VALUES (0,-100,'Snowflakes · Бадминтон',NULL)")
    store.db.close()
    server = make_server(path, TOKEN, {11}, None, port=0)
    print(json.dumps({'url': 'http://127.0.0.1:{}'.format(server.server_port), 'auth': signed()}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
