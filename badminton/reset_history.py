"""Explicit, backed-up reset of training history. Directories and group stay intact."""
import argparse
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path


HISTORY_TABLES = (
    'publication_messages', 'publications', 'payments', 'transfers',
    'court_payments', 'participants', 'trainings',
)


def reset_history(path, confirm=False):
    path = Path(path).resolve()
    # mode=rw prevents a typo from silently creating an empty database.
    db = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=15)
    backup_path = None
    try:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT value FROM settings WHERE key='shared_workspace'").fetchone() != ('1',):
            raise ValueError('Сначала обновите базу до общей рабочей области.')
        counts = {table: db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                  for table in HISTORY_TABLES}
        if not confirm:
            return {'dry_run': True, 'counts': counts}
        # A selected/in-flight outbox message remains present until delivery completes.
        # Refuse to race its delivery or reuse its ID; never discard pending messages.
        if db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]:
            raise ValueError('В очереди есть сообщения. Дождитесь отправки и повторите команду.')
        backup_path = path.with_name(path.name + '.before-reset-' +
                                    datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
        fd = os.open(str(backup_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        # A second read connection gives backup() a committed snapshot while our
        # write reservation prevents other writers changing that snapshot.
        source = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        target = sqlite3.connect(str(backup_path))
        try:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Проверка резервной копии не прошла; сброс отменён.')
        finally:
            source.close()
            target.close()
        for table in HISTORY_TABLES:
            db.execute('DELETE FROM ' + table)
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='miniapp_requests'").fetchone():
            db.execute('DELETE FROM miniapp_requests WHERE tid<>0')
        # Keep generations monotonic: deleting sessions would make old buttons valid.
        db.execute("UPDATE sessions SET generation=generation+1,state='{}'")
        db.execute("UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='shared_revision'")
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Проверка связей не прошла; сброс отменён.')
        db.commit()
        return {'dry_run': False, 'deleted': counts, 'backup': str(backup_path),
                'remaining_trainings': 0}
    finally:
        db.rollback()
        db.close()


def main():
    parser = argparse.ArgumentParser(description='Сброс истории с резервной копией; справочники и группа сохраняются.')
    parser.add_argument('--database', default=os.environ.get('DATABASE_PATH', 'data/badminton.sqlite3'))
    parser.add_argument('--confirm', choices=['DELETE_ALL_TRAININGS'],
                        help='Без этого флага команда только показывает количество записей.')
    args = parser.parse_args()
    try:
        result = reset_history(args.database, confirm=args.confirm == 'DELETE_ALL_TRAININGS')
    except (ValueError, sqlite3.Error, OSError) as error:
        parser.exit(1, 'Сброс не выполнен: {}\n'.format(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
