import unittest

from badminton.defaults import LOCATIONS, PARTICIPANTS
from badminton.store import Store


class DefaultDirectoryTests(unittest.TestCase):
    def test_replace_removes_old_directories_without_breaking_training_references(self):
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        with store.db:
            pid = store.person(11, "Старый игрок")
            store.person(11, "Неиспользуемый игрок")
            lid = store.location(11, "Старый зал")
            store.location(11, "Неиспользуемый зал")
            tid = store.execute("INSERT INTO trainings(owner,date,location_id,court_cost) VALUES (?,?,?,?)", (11, "2026-09-25", lid, 0)).lastrowid
            store.add_participant(11, tid, pid)
        store.replace_directories_with_defaults({11})
        self.assertEqual(tuple(r[0] for r in store.all("SELECT name FROM people WHERE saved=1 ORDER BY id")), PARTICIPANTS)
        self.assertEqual(tuple(r[0] for r in store.all("SELECT name FROM locations WHERE active=1 ORDER BY id")), LOCATIONS)
        self.assertIsNone(store.one("SELECT id FROM people WHERE name='Неиспользуемый игрок'"))
        self.assertIsNone(store.one("SELECT id FROM locations WHERE name='Неиспользуемый зал'"))
        self.assertEqual(store.participants(tid)[0]["name"], "Старый игрок")
        self.assertEqual(store.training(11, tid)["location"], "Старый зал")

    def test_defaults_for_each_organizer_in_requested_order(self):
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        store.seed_defaults({11, 22})
        for owner in (11, 22):
            self.assertEqual(tuple(r[0] for r in store.all(
                "SELECT name FROM people WHERE owner=? AND saved=1 ORDER BY id", (owner,))), PARTICIPANTS)
            self.assertEqual(tuple(r[0] for r in store.all(
                "SELECT name FROM locations WHERE owner=? ORDER BY id", (owner,))), LOCATIONS)
        self.assertEqual(store.one("SELECT COUNT(*) FROM participants")[0], 0)
        self.assertEqual(store.one("SELECT COUNT(*) FROM trainings")[0], 0)

    def test_repeated_seed_preserves_existing_entries_and_guests(self):
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        with store.db:
            existing = store.person(11, "Рамиля")
            custom = store.person(11, "Другой игрок")
            guest = store.person(11, "Настя", saved=False)
            location = store.location(11, "Baam")
            store.location(11, "Другой зал")
        store.seed_defaults({11})
        store.seed_defaults({11})
        self.assertEqual(store.one("SELECT COUNT(*) FROM people WHERE saved=1")[0], 21)
        self.assertEqual(store.one("SELECT COUNT(*) FROM locations")[0], 4)
        self.assertEqual(store.person(11, "Рамиля"), existing)
        self.assertEqual(store.person(11, "Другой игрок"), custom)
        self.assertEqual(store.location(11, "Baam"), location)
        self.assertEqual(store.person_owned(11, guest)["saved"], 0)
