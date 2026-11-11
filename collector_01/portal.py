"""运营侧门户库投影。

portal_database 与采集状态库分离：每次运行把当前活动记录全量 upsert 到门户，
待处理通知清单单独持久化，供运营追溯处理。
"""

import json
import sqlite3
from pathlib import Path


class PortalStore:
    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self):
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS portal_events (
                    activity_id TEXT PRIMARY KEY,
                    title TEXT,
                    city TEXT,
                    status TEXT,
                    venue TEXT,
                    absent INTEGER NOT NULL DEFAULT 0,
                    provenance TEXT NOT NULL,
                    updated_run_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS portal_notifications (
                    id INTEGER PRIMARY KEY,
                    activity_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    dedup_key TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def replace_events(self, events, run_id, updated_at):
        with self.connect() as connection:
            for event in events:
                connection.execute(
                    "INSERT INTO portal_events"
                    "(activity_id, title, city, status, venue, absent, "
                    "provenance, updated_run_id, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(activity_id) DO UPDATE SET "
                    "title = excluded.title, city = excluded.city, "
                    "status = excluded.status, venue = excluded.venue, "
                    "absent = excluded.absent, provenance = excluded.provenance, "
                    "updated_run_id = excluded.updated_run_id, "
                    "updated_at = excluded.updated_at",
                    (
                        event["id"],
                        event.get("title"),
                        event.get("city"),
                        event.get("status"),
                        event.get("venue"),
                        1 if event.get("absent") else 0,
                        json.dumps(event.get("provenance", {}), ensure_ascii=False, sort_keys=True),
                        run_id,
                        updated_at,
                    ),
                )

    def add_notification(self, activity_id, kind, dedup_key, payload, run_id, created_at):
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO portal_notifications"
                "(activity_id, kind, dedup_key, payload, run_id, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    activity_id,
                    kind,
                    dedup_key,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    run_id,
                    created_at,
                ),
            )
            return cursor.rowcount == 1

    def list_notifications(self):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT activity_id, kind, dedup_key, payload, run_id "
                "FROM portal_notifications ORDER BY id"
            ).fetchall()
        return [
            {
                "id": row["activity_id"],
                "kind": row["kind"],
                "dedup_key": row["dedup_key"],
                "payload": json.loads(row["payload"]),
                "run_id": row["run_id"],
            }
            for row in rows
        ]
