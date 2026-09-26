"""Native Telegram attendance polls and immutable-at-import training rosters."""
import json
import re
from datetime import datetime, timedelta, timezone

from .calculator import ValidationError
from .store import clean_name
from .telegram import TelegramError

DEFAULT_OPTIONS = ['19:00-21:00', 'Thinking', 'No']
YES_OPTIONS = {'yes', 'да', '19:00-21:00', '19:00', '18:00-20:00'}
TZ = timezone(timedelta(hours=7))
SCHEMA = """
CREATE TABLE IF NOT EXISTS attendance_polls (
 id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER NOT NULL, creator INTEGER NOT NULL,
 chat_id INTEGER NOT NULL, thread_id INTEGER, question TEXT NOT NULL,
 training_date TEXT NOT NULL, location_id INTEGER REFERENCES locations(id),
 options TEXT NOT NULL, created_at INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued', telegram_id TEXT UNIQUE, message_id INTEGER,
 closed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS poll_votes (
 poll_id INTEGER REFERENCES attendance_polls(id), user_id INTEGER NOT NULL,
 name TEXT NOT NULL, choices TEXT NOT NULL, PRIMARY KEY(poll_id,user_id));
CREATE TABLE IF NOT EXISTS telegram_people (
 owner INTEGER NOT NULL, user_id INTEGER NOT NULL, person_id INTEGER REFERENCES people(id),
 PRIMARY KEY(owner,user_id), UNIQUE(owner,person_id));
CREATE TABLE IF NOT EXISTS poll_imports (
 poll_id INTEGER REFERENCES attendance_polls(id),
 training_id INTEGER UNIQUE REFERENCES trainings(id) ON DELETE CASCADE,
 PRIMARY KEY(poll_id,training_id));
"""


def normalize(text):
    return ''.join(text.split()).casefold()


def poll_date(question, now=None):
    """Resolve dd/mm or dd.mm to the closest valid date around the publication year."""
    now = now or datetime.now(TZ).date()
    matches = re.findall(r'(?<![\d./])(\d{2})([/.])(\d{2})(?![\d./])', question)
    if len(matches) != 1:
        raise ValidationError('В заголовке нужна одна дата в формате дд/мм или дд.мм, например Baam · 26/09.')
    day, _, month = matches[0]
    candidates = []
    for year in (now.year-1, now.year, now.year+1):
        try:
            candidates.append(now.replace(year=year, month=int(month), day=int(day)))
        except ValueError:
            pass
    if not candidates:
        raise ValidationError('В заголовке указана несуществующая дата.')
    return min(candidates, key=lambda d: (abs((d-now).days), d < now)).isoformat()


def validate(question, options):
    if not isinstance(question, str):
        raise ValidationError('Укажите заголовок голосования.')
    question = ' '.join(question.split())
    if not 1 <= len(question) <= 300:
        raise ValidationError('Заголовок должен содержать от 1 до 300 символов.')
    date = poll_date(question)
    if not isinstance(options, list) or not 2 <= len(options) <= 12:
        raise ValidationError('Укажите от 2 до 12 вариантов ответа, каждый с новой строки.')
    if not all(isinstance(o, str) for o in options):
        raise ValidationError('Варианты ответа должны быть текстом.')
    options = [' '.join(o.split()) for o in options]
    if any(not 1 <= len(o) <= 100 for o in options) or len(options) < 2:
        raise ValidationError('Каждый вариант должен содержать от 1 до 100 символов.')
    if len({normalize(o) for o in options}) != len(options):
        raise ValidationError('Варианты ответа не должны повторяться.')
    return question, options, date


def queue(store, owner, uid, question, options):
    question, options, date = validate(question, options)
    group = store.one('SELECT * FROM group_bindings WHERE owner=?', (owner,))
    if not group:
        raise ValidationError('Сначала подключите группу командой /bind.')
    locations = [r for r in store.all('SELECT * FROM locations WHERE owner=? AND active=1', (owner,))
                 if re.search(r'(?<!\w)' + re.escape(r['name']) + r'(?!\w)', question, re.I)]
    location_id = locations[0]['id'] if len(locations) == 1 else None
    pid = store.execute('''INSERT INTO attendance_polls
        (owner,creator,chat_id,thread_id,question,training_date,location_id,options,created_at)
        VALUES (?,?,?,?,?,?,?,?,?)''', (owner, uid, group['chat_id'], group['thread_id'], question,
        date, location_id, json.dumps([{'text': o, 'voter_count': 0} for o in options]), int(datetime.now(TZ).timestamp()))).lastrowid
    store.enqueue('sendAttendancePoll', {'id': pid})
    return pid


def deliver(store, api, pid):
    row = store.one('SELECT * FROM attendance_polls WHERE id=?', (pid,))
    if not row or row['status'] != 'queued':
        # If a process died after sendPoll, retrying could create another poll.
        if row and row['status'] == 'sending':
            failure(store, pid, 'Доставка не подтверждена. Проверьте группу перед созданием нового опроса.')
        return
    payload = {'chat_id': row['chat_id'], 'question': row['question'],
               'options': [{'text': o['text']} for o in json.loads(row['options'])],
               'is_anonymous': False, 'type': 'regular', 'allows_multiple_answers': False,
               'allows_revoting': True, 'allow_adding_options': True,
               'hide_results_until_closes': False}
    if row['thread_id']:
        payload['message_thread_id'] = row['thread_id']
    with store.db:
        store.execute("UPDATE attendance_polls SET status='sending' WHERE id=?", (pid,))
    try:
        message = api.call('sendPoll', payload)
    except TelegramError as error:
        if error.code == 429:
            with store.db:
                store.execute("UPDATE attendance_polls SET status='queued' WHERE id=?", (pid,))
            raise
        failure(store, pid, 'Telegram не подтвердил отправку. Проверьте группу и права бота перед повторным созданием.')
        return
    with store.db:
        poll = message['poll']
        store.execute("UPDATE attendance_polls SET status='sent',telegram_id=?,message_id=?,options=? WHERE id=?",
                      (poll['id'], message['message_id'], json.dumps(poll['options']), pid))
        store.enqueue('sendMessage', {'chat_id': row['creator'], 'text': 'Голосование «{}» опубликовано. Ответы видны в группе; участники могут менять голос и добавлять варианты.'.format(row['question'])})


def failure(store, pid, reason):
    with store.db:
        row = store.one('SELECT creator FROM attendance_polls WHERE id=?', (pid,))
        store.execute("UPDATE attendance_polls SET status='failed' WHERE id=?", (pid,))
        store.enqueue('sendMessage', {'chat_id': row['creator'], 'text': reason})


def receive(store, update):
    poll = update.get('poll')
    if poll:
        row = store.one('SELECT id FROM attendance_polls WHERE telegram_id=?', (poll['id'],))
        if row:
            store.execute('UPDATE attendance_polls SET options=?,closed=? WHERE id=?',
                          (json.dumps(poll['options']), int(poll['is_closed']), row['id']))
        return
    answer = update.get('poll_answer', {})
    row = store.one('SELECT * FROM attendance_polls WHERE telegram_id=?', (answer.get('poll_id'),))
    user = answer.get('user')
    if not row or not user or user.get('is_bot'):
        return
    name = ' '.join(filter(None, [user.get('first_name'), user.get('last_name')])) or 'Участник {}'.format(user['id'])
    # Persistent option IDs survive adding/reordering options. Legacy updates retain indexes.
    choices = ({'persistent': answer['option_persistent_ids']} if 'option_persistent_ids' in answer
               else {'indexes': answer.get('option_ids', [])})
    store.execute('INSERT OR REPLACE INTO poll_votes VALUES (?,?,?,?)',
                  (row['id'], user['id'], name[:60], json.dumps(choices)))


def chosen(options, choices):
    choices = json.loads(choices)
    if 'persistent' in choices:
        return [o for o in options if o.get('persistent_id') in choices['persistent']]
    return [o for index, o in enumerate(options) if index in choices.get('indexes', [])]


def view(store, row):
    result = dict(row)
    options = json.loads(row['options'])
    result['options'] = [{**o, 'voters': []} for o in options]
    for vote in store.all('SELECT * FROM poll_votes WHERE poll_id=? ORDER BY name,user_id', (row['id'],)):
        selected = chosen(options, vote['choices'])
        for index, option in enumerate(options):
            if option in selected:
                result['options'][index]['voters'].append({'id': vote['user_id'], 'name': vote['name']})
    if row['message_id'] and str(row['chat_id']).startswith('-100'):
        result['url'] = 'https://t.me/c/{}/{}'.format(str(row['chat_id'])[4:], row['message_id'])
    return result


def latest(store, owner):
    group = store.one('SELECT chat_id FROM group_bindings WHERE owner=?', (owner,))
    if not group:
        return None
    row = store.one("SELECT * FROM attendance_polls WHERE owner=? AND chat_id=? AND status='sent' ORDER BY message_id DESC,id DESC LIMIT 1", (owner, group['chat_id']))
    if not row:
        return None
    result = view(store, row)
    existing = store.one('SELECT id FROM trainings WHERE owner=? AND date=? ORDER BY id LIMIT 1', (owner, row['training_date']))
    result['existing_training_id'] = existing['id'] if existing else None
    result['participants'] = list({v['id']: v for o in result['options'] if normalize(o['text']) in YES_OPTIONS for v in o['voters']}.values())
    result['incomplete'] = any(len(o['voters']) < o.get('voter_count', len(o['voters']))
                               for o in result['options'] if normalize(o['text']) in YES_OPTIONS)
    known = {o.get('persistent_id') for o in result['options']}
    for vote in store.all('SELECT choices FROM poll_votes WHERE poll_id=?', (row['id'],)):
        choices = json.loads(vote['choices'])
        if any(key not in known for key in choices.get('persistent', [])):
            result['incomplete'] = True
    return result


def create_training(store, owner, uid, date, lid, cost, poll_id=None):
    candidate = None
    if poll_id:
        candidate = latest(store, owner)
        if not candidate or candidate['id'] != int(poll_id) or candidate['training_date'] != date:
            raise ValidationError('Голосование изменилось или дата не совпадает. Откройте «Новую тренировку» заново.')
        if candidate['existing_training_id']:
            raise ValidationError('Тренировка на эту дату уже существует. Откройте историю.')
        if candidate['incomplete']:
            raise ValidationError('Ещё не все голоса получены. Обновите данные через несколько секунд или создайте тренировку вручную.')
    tid = store.execute('INSERT INTO trainings(owner,date,location_id,court_cost,created_by) VALUES (?,?,?,?,?)',
                        (owner, date, lid, cost, uid)).lastrowid
    if candidate:
        for voter in candidate['participants']:
            mapping = store.one('SELECT person_id FROM telegram_people WHERE owner=? AND user_id=?', (owner, voter['id']))
            if mapping:
                person_id = mapping['person_id']
            else:
                name = clean_name(voter['name'])
                person = store.one('SELECT id FROM people WHERE owner=? AND name_key=? AND saved=1', (owner, name.casefold()))
                if person and store.one('SELECT 1 FROM telegram_people WHERE owner=? AND person_id=?', (owner, person['id'])):
                    name = name[:38] + ' · ' + str(voter['id'])
                person_id = store.person(owner, name)
                store.execute('INSERT INTO telegram_people VALUES (?,?,?)', (owner, voter['id'], person_id))
            store.add_participant(owner, tid, person_id)
        store.execute('INSERT INTO poll_imports VALUES (?,?)', (candidate['id'], tid))
    return tid
