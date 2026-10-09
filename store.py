"""SQLite persistence; migrations preserve existing clients, posts and plans."""
from pathlib import Path
import sqlite3


class Store:
    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS posts (
                id TEXT PRIMARY KEY, platform TEXT NOT NULL, content TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'draft',
                scheduled_at TEXT, created_at TEXT NOT NULL, published_at TEXT,
                remote_id TEXT, error TEXT
            );
            CREATE TABLE IF NOT EXISTS clients (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, niche TEXT NOT NULL
            );
        """)
        columns = {row["name"] for row in self.query("PRAGMA table_info(posts)")}
        for name, definition in {
            "client_id": "TEXT NOT NULL DEFAULT 'default'",
            "revision": "INTEGER NOT NULL DEFAULT 1",
        }.items():
            if name not in columns:
                self.execute(f"ALTER TABLE posts ADD COLUMN {name} {definition}")
        self.execute("INSERT OR IGNORE INTO clients VALUES(?,?,?)",
                     ("default", "My brand", "Tech, football & motivation"))
        self.execute("UPDATE posts SET status='failed',error=? WHERE status='publishing'",
                     ("Publishing was interrupted. Check the social account before retrying.",))

    def query(self, sql, params=()):
        return [dict(row) for row in self.db.execute(sql, params).fetchall()]

    def one(self, sql, params=()):
        row = self.db.execute(sql, params).fetchone()
        return dict(row) if row else None

    def execute(self, sql, params=()):
        return self.db.execute(sql, params)

    def get(self, post_id):
        return self.one("SELECT * FROM posts WHERE id=?", (post_id,))

    def close(self):
        self.db.close()
