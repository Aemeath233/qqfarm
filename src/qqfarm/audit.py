import json
import logging
import secrets
import sqlite3
import time
from hashlib import sha256
from pathlib import Path

from qqfarm.config import FarmError

logger = logging.getLogger(__name__)


def tag(value):
    return sha256(str(value).encode()).hexdigest()[:10]


class Audit:
    def __init__(self, path: Path, *, check_same_thread=True):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.instance = secrets.token_hex(6)
        self.account = ""
        self.on_record = None
        self.db = sqlite3.connect(path, check_same_thread=check_same_thread)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS operations (
            id INTEGER PRIMARY KEY, at REAL, instance TEXT, account TEXT,
            operation TEXT, state TEXT, details TEXT
        )""")
        # SQLite can reuse INTEGER PRIMARY KEY values after retention clears the table.
        # Keep an independent sequence so the friend statistics cursor never skips new rows.
        floor = self.db.execute("SELECT COALESCE(MAX(id),0) FROM operations").fetchone()[0]
        if self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='friend_stats_cursor'"
        ).fetchone():
            cursor = self.db.execute("SELECT MAX(cursor) FROM friend_stats_cursor").fetchone()[0]
            floor = max(floor, cursor or 0)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS audit_sequence (id INTEGER PRIMARY KEY, last_id INTEGER)"
        )
        self.db.execute("INSERT OR IGNORE INTO audit_sequence VALUES(1,?)", (floor,))
        self.db.execute("UPDATE audit_sequence SET last_id=MAX(last_id,?) WHERE id=1", (floor,))
        self.db.commit()
        self.last_prune = 0.0
        self.prune()

    def prune(self):
        now = time.time()
        if now - self.last_prune < 3600:
            return
        with self.db:
            self.db.execute("DELETE FROM operations WHERE at<?", (now - 7 * 86400,))
        self.last_prune = now

    def record(self, operation, state, **details):
        encoded = json.dumps(details, ensure_ascii=False, separators=(",", ":"))
        self.prune()
        with self.db:
            self.db.execute("UPDATE audit_sequence SET last_id=last_id+1 WHERE id=1")
            identifier = self.db.execute(
                "SELECT last_id FROM audit_sequence WHERE id=1"
            ).fetchone()[0]
            self.db.execute(
                "INSERT INTO operations (id,at,instance,account,operation,state,details) "
                "VALUES (?,?,?,?,?,?,?)",
                (identifier, time.time(), self.instance, self.account, operation, state, encoded),
            )
        logger.info("账号=%s 操作=%s 状态=%s %s", self.account, operation, state, encoded)
        if self.on_record:
            self.on_record(operation, state, details)

    def close(self):
        self.db.close()


class InstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0)
        if not self.handle.read(1):
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if __import__("os").name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise FarmError("当前目录已有挂机实例正在运行。") from None
        return self

    def __exit__(self, *args):
        self.handle.close()
