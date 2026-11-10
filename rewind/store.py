import json
import sqlite3
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL,
            detail TEXT NOT NULL, time TEXT NOT NULL, dedupe TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS operations(key TEXT PRIMARY KEY, task_id TEXT,
            kind TEXT, status TEXT, result TEXT, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS provider_orders(id TEXT PRIMARY KEY, body TEXT NOT NULL);
        """)

    def save(self, task):
        with self.db:
            self.db.execute("INSERT INTO tasks VALUES (?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                            (task['id'], json.dumps(task)))
        return task

    def get(self, task_id):
        row = self.db.execute("SELECT body FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise KeyError(task_id)
        return json.loads(row[0])

    def tasks(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT body FROM tasks ORDER BY rowid DESC")]

    def event(self, task_id, kind, source, detail, dedupe=None):
        with self.db:
            cur = self.db.execute("INSERT OR IGNORE INTO events(task_id,kind,source,detail,time,dedupe) VALUES(?,?,?,?,?,?)",
                                  (task_id, kind, source, json.dumps(detail), now(), dedupe))
        return cur.rowcount == 1

    def events(self, task_id):
        return [{**dict(r), 'detail': json.loads(r['detail'])} for r in self.db.execute(
            "SELECT * FROM events WHERE task_id=? ORDER BY seq", (task_id,))]

    def operation(self, key, task_id, kind):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO operations VALUES(?,?,?,'pending',NULL,?)",
                            (key, task_id, kind, now()))
        r = dict(self.db.execute("SELECT * FROM operations WHERE key=?", (key,)).fetchone())
        r['result'] = json.loads(r['result']) if r['result'] else None
        return r

    def finish(self, key, result):
        with self.db:
            self.db.execute("UPDATE operations SET status='done',result=? WHERE key=?", (json.dumps(result), key))

