"""Persistent counts from confirmed operations, scoped to account and Beijing day."""

import json
import re
from datetime import datetime, timedelta, timezone

KINDS = ("help", "steal", "insects", "weeds")
BEIJING = timezone(timedelta(hours=8))
KNOWN_SKIP_CODES = {"help": 1001057, "steal": 1001040}
DAILY_LIMIT_CODE = 1001046
ACTION_METHODS = {
    "help": "Farming",
    "steal": "Harvest",
    "insects": "PutInsects",
    "weeds": "PutWeeds",
}


def day_key(stamp):
    return datetime.fromtimestamp(stamp, BEIJING).date().isoformat()


class FriendStats:
    def __init__(self, db):
        self.db = db
        db.executescript("""
            CREATE TABLE IF NOT EXISTS friend_daily (
                account TEXT, day TEXT, friend TEXT, kind TEXT,
                calls INTEGER NOT NULL, plots INTEGER NOT NULL,
                water INTEGER NOT NULL, weeds INTEGER NOT NULL, insects INTEGER NOT NULL,
                PRIMARY KEY(account, day, friend, kind)
            );
            CREATE TABLE IF NOT EXISTS friend_stats_cursor (id INTEGER PRIMARY KEY, cursor INTEGER);
            INSERT OR IGNORE INTO friend_stats_cursor VALUES(1,0);
            CREATE TABLE IF NOT EXISTS friend_blocks (
                account TEXT, kind TEXT, day TEXT, reason TEXT,
                PRIMARY KEY(account,kind,day)
            );
        """)
        self.update()
        self.migrate_known_blocks()

    def migrate_known_blocks(self):
        """Clear old blocks whose ambiguous batch replies now have safe skip handling."""
        rows = self.db.execute("SELECT account,kind,day,reason FROM friend_blocks").fetchall()
        with self.db:
            for account, kind, day, reason in rows:
                codes = set(int(v) for v in re.findall(r"\b100\d{4}\b", reason))
                if KNOWN_SKIP_CODES.get(kind) in codes:
                    self.db.execute(
                        "DELETE FROM friend_blocks WHERE account=? AND kind=? AND day=?",
                        (account, kind, day),
                    )
                elif day == "*" and (
                    (kind == "help" and reason.startswith("照料回包未完整确认"))
                    or (
                        kind == "insects"
                        and reason.startswith("好友放虫")
                        and ("未能确认全部目标" in reason or "结果无法复查" in reason)
                    )
                    or (
                        kind == "weeds"
                        and reason.startswith("好友放草")
                        and ("未能确认全部目标" in reason or "结果无法复查" in reason)
                    )
                ):
                    self.db.execute(
                        "DELETE FROM friend_blocks WHERE account=? AND kind=? AND day=?",
                        (account, kind, day),
                    )
                elif DAILY_LIMIT_CODE in codes and day == "*" and kind in ACTION_METHODS:
                    # Use the original rejection's day, not today's restart day.
                    # If retention removed that record, this old daily cap has expired.
                    previous_day = None
                    for stamp, encoded in self.db.execute(
                        "SELECT at,details FROM operations WHERE account=? AND operation=? "
                        "AND state='rejected' ORDER BY id DESC",
                        (account, "rpc_" + ACTION_METHODS[kind]),
                    ):
                        if json.loads(encoded).get("code") == DAILY_LIMIT_CODE:
                            previous_day = day_key(stamp)
                            break
                    self.db.execute(
                        "DELETE FROM friend_blocks WHERE account=? AND kind=? AND day='*'",
                        (account, kind),
                    )
                    if previous_day:
                        self.db.execute(
                            "INSERT OR REPLACE INTO friend_blocks VALUES(?,?,?,?)",
                            (
                                account,
                                kind,
                                previous_day,
                                "游戏判定今日次数已达上限；次日自动恢复。",
                            ),
                        )

    def update(self):
        cursor = self.db.execute("SELECT cursor FROM friend_stats_cursor WHERE id=1").fetchone()[0]
        rows = self.db.execute(
            "SELECT id,at,account,operation,state,details FROM operations WHERE id>? ORDER BY id",
            (cursor,),
        ).fetchall()
        if not rows:
            return
        with self.db:
            for _, stamp, account, operation, state, encoded in rows:
                kind = operation.removeprefix("friend_")
                if state != "confirmed" or kind not in KINDS:
                    continue
                details = json.loads(encoded)
                friend = details.get("friend")
                if not friend:
                    continue
                care = details.get("care_counts", {})
                self.db.execute(
                    "INSERT INTO friend_daily VALUES(?,?,?,?,1,?,?,?,?) "
                    "ON CONFLICT(account,day,friend,kind) DO UPDATE SET "
                    "calls=calls+1, plots=plots+excluded.plots, water=water+excluded.water, "
                    "weeds=weeds+excluded.weeds, insects=insects+excluded.insects",
                    (
                        account,
                        day_key(stamp),
                        friend,
                        kind,
                        len(details.get("plots", [])),
                        care.get("water", 0),
                        care.get("weeds", 0),
                        care.get("insects", 0),
                    ),
                )
            self.db.execute("UPDATE friend_stats_cursor SET cursor=? WHERE id=1", (rows[-1][0],))

    def snapshot(self, account, now):
        rows = self.db.execute(
            "SELECT friend,kind,calls,plots,water,weeds,insects FROM friend_daily "
            "WHERE account=? AND day=?",
            (account, day_key(now)),
        ).fetchall()
        friends = {}
        totals = {kind: {"calls": 0, "plots": 0} for kind in KINDS}
        for friend, kind, calls, plots, water, weeds, insects in rows:
            friends.setdefault(friend, {})[kind] = {
                "calls": calls,
                "plots": plots,
                "water": water,
                "weeds": weeds,
                "insects": insects,
            }
            totals[kind]["calls"] += calls
            totals[kind]["plots"] += plots
        all_time = {}
        for friend, kind, calls, plots in self.db.execute(
            "SELECT friend,kind,SUM(calls),SUM(plots) FROM friend_daily "
            "WHERE account=? GROUP BY friend,kind",
            (account,),
        ).fetchall():
            all_time.setdefault(friend, {})[kind] = {"calls": calls, "plots": plots}
        return {"day": day_key(now), "friends": friends, "totals": totals, "all_time": all_time}

    def block(self, account, kind, reason, *, day="*"):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO friend_blocks VALUES(?,?,?,?)",
                (account, kind, day, reason),
            )

    def blocks(self, account, now):
        return dict(
            self.db.execute(
                "SELECT kind,reason FROM friend_blocks WHERE account=? AND day IN ('*',?)",
                (account, day_key(now)),
            ).fetchall()
        )

    def daily_blocks(self, account, now):
        return dict(
            self.db.execute(
                "SELECT kind,reason FROM friend_blocks WHERE account=? AND day=?",
                (account, day_key(now)),
            ).fetchall()
        )

    def resume(self, account, kind):
        with self.db:
            self.db.execute("DELETE FROM friend_blocks WHERE account=? AND kind=?", (account, kind))
