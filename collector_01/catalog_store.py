"""--run 工作流的 SQLite 状态库。

与原始 --capture 的 source_records 表共存，互不影响。
所有业务导入按指纹去重：同一 (route, revision, authority, id, 字段集合) 重复
导入不会产生新的业务版本，多次进程执行共享同一 database 时天然幂等。
"""

import json
import sqlite3
from pathlib import Path

CATALOG_SCOPE = "default"


def observation_fingerprint(observation, route, observed_at):
    # 业务版本身份只看路由与站点字段内容；observed_at 不参与，这样不同进程在
    # 同一时刻观测到相同页面时不会重复导入业务版本。每次请求的时间证据由
    # catalog_requests 完整保留，来源追溯不丢失。
    canonical = json.dumps(
        {
            "id": observation["id"],
            "revision": observation["revision"],
            "authority": observation["authority"],
            "fields": observation["fields"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{route}\0{canonical}"


class CatalogStore:
    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self):
        with self.connect() as connection:
            self._create_schema(connection)

    def _create_schema(self, connection):
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS catalog_runs (
                run_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                name TEXT,
                mode TEXT NOT NULL,
                coverage TEXT NOT NULL,
                retry_after INTEGER,
                budget_total INTEGER NOT NULL,
                budget_used INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                sources TEXT NOT NULL,
                PRIMARY KEY (run_id, attempt)
            );
            CREATE TABLE IF NOT EXISTS catalog_requests (
                id INTEGER PRIMARY KEY,
                run_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                route TEXT NOT NULL,
                status_code INTEGER NOT NULL,
                scheduled_at TEXT NOT NULL,
                observed_at TEXT,
                retry_after INTEGER,
                seq INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS catalog_observations (
                fingerprint TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                activity_id TEXT NOT NULL,
                route TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                revision INTEGER NOT NULL,
                authority TEXT NOT NULL,
                fields TEXT NOT NULL,
                imported_at TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_obs_activity
                ON catalog_observations(activity_id);
            CREATE TABLE IF NOT EXISTS catalog_events (
                activity_id TEXT PRIMARY KEY,
                absent INTEGER NOT NULL DEFAULT 0,
                first_run_id TEXT NOT NULL,
                last_seen_run_id TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS catalog_notifications (
                id INTEGER PRIMARY KEY,
                activity_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                dedup_key TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL,
                run_id TEXT NOT NULL,
                created_at TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS catalog_conflicts (
                id INTEGER PRIMARY KEY,
                activity_id TEXT NOT NULL,
                field TEXT NOT NULL,
                revision INTEGER NOT NULL,
                evidence TEXT NOT NULL,
                resolved INTEGER NOT NULL DEFAULT 0,
                updated_run_id TEXT NOT NULL,
                UNIQUE (activity_id, field)
            );
            CREATE TABLE IF NOT EXISTS catalog_checkpoints (
                scope TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

    # ----- runs -----------------------------------------------------------
    def begin_run(self, run_id, name, mode, budget_total, started_at, sources):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(attempt), 0) AS last_attempt "
                "FROM catalog_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            attempt = row["last_attempt"] + 1
            connection.execute(
                "INSERT INTO catalog_runs"
                "(run_id, attempt, name, mode, coverage, retry_after, "
                "budget_total, budget_used, started_at, finished_at, sources) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    attempt,
                    name,
                    mode,
                    "running",
                    None,
                    budget_total,
                    0,
                    started_at,
                    None,
                    json.dumps(sources, ensure_ascii=False),
                ),
            )
        return attempt

    def finish_run(self, run_id, attempt, coverage, retry_after, budget_used, finished_at):
        with self.connect() as connection:
            connection.execute(
                "UPDATE catalog_runs SET coverage = ?, retry_after = ?, "
                "budget_used = ?, finished_at = ? "
                "WHERE run_id = ? AND attempt = ?",
                (coverage, retry_after, budget_used, finished_at, run_id, attempt),
            )

    # ----- request evidence ----------------------------------------------
    def record_request(self, run_id, attempt, seq, route, status_code,
                       scheduled_at, observed_at, retry_after):
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO catalog_requests"
                "(run_id, attempt, route, status_code, scheduled_at, "
                "observed_at, retry_after, seq) VALUES (?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    attempt,
                    route,
                    status_code,
                    scheduled_at,
                    observed_at,
                    retry_after,
                    seq,
                ),
            )

    def fetch_requests(self, run_id, attempt):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT route, status_code, scheduled_at, observed_at, "
                "retry_after, seq FROM catalog_requests "
                "WHERE run_id = ? AND attempt = ? ORDER BY seq",
                (run_id, attempt),
            ).fetchall()
        return [dict(row) for row in rows]

    # ----- observations ---------------------------------------------------
    def insert_observation(self, run_id, observation, route, observed_at):
        """按指纹幂等导入一条观测。返回 True 表示新增业务证据。"""
        fingerprint = observation_fingerprint(observation, route, observed_at)
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO catalog_observations"
                "(fingerprint, run_id, activity_id, route, observed_at, "
                "revision, authority, fields) VALUES (?,?,?,?,?,?,?,?)",
                (
                    fingerprint,
                    run_id,
                    observation["id"],
                    route,
                    observed_at,
                    observation["revision"],
                    observation["authority"],
                    json.dumps(observation["fields"], ensure_ascii=False, sort_keys=True),
                ),
            )
            return cursor.rowcount == 1

    def load_observations(self):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT activity_id, route, observed_at, revision, authority, "
                "fields, run_id FROM catalog_observations "
                "ORDER BY activity_id, revision, route"
            ).fetchall()
        result = []
        for row in rows:
            result.append(
                {
                    "id": row["activity_id"],
                    "route": row["route"],
                    "observed_at": row["observed_at"],
                    "revision": row["revision"],
                    "authority": row["authority"],
                    "fields": json.loads(row["fields"]),
                    "run_id": row["run_id"],
                }
            )
        return result

    def run_present_ids(self, run_id):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT activity_id FROM catalog_observations "
                "WHERE run_id = ? ORDER BY activity_id",
                (run_id,),
            ).fetchall()
        return [row["activity_id"] for row in rows]

    # ----- event lifecycle ------------------------------------------------
    def upsert_event(self, activity_id, run_id, absent, updated_at):
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO catalog_events"
                "(activity_id, absent, first_run_id, last_seen_run_id, updated_at) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(activity_id) DO UPDATE SET "
                "absent = excluded.absent, "
                "last_seen_run_id = excluded.last_seen_run_id, "
                "updated_at = excluded.updated_at",
                (activity_id, 1 if absent else 0, run_id, run_id, updated_at),
            )

    def get_event(self, activity_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT activity_id, absent, first_run_id, last_seen_run_id "
                "FROM catalog_events WHERE activity_id = ?",
                (activity_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_event_ids(self):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT activity_id FROM catalog_events ORDER BY activity_id"
            ).fetchall()
        return [row["activity_id"] for row in rows]

    def mark_absent(self, present_ids, run_id, updated_at):
        with self.connect() as connection:
            connection.execute(
                "UPDATE catalog_events SET absent = 1, updated_at = ?",
                (updated_at,),
            )
            if present_ids:
                placeholders = ",".join("?" for _ in present_ids)
                connection.execute(
                    f"UPDATE catalog_events SET absent = 0, "
                    f"last_seen_run_id = ?, updated_at = ? "
                    f"WHERE activity_id IN ({placeholders})",
                    (run_id, updated_at, *present_ids),
                )

    # ----- notifications --------------------------------------------------
    def add_notification(self, activity_id, kind, dedup_key, payload, run_id):
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO catalog_notifications"
                "(activity_id, kind, dedup_key, payload, run_id) "
                "VALUES (?,?,?,?,?)",
                (
                    activity_id,
                    kind,
                    dedup_key,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    run_id,
                ),
            )
            return cursor.rowcount == 1

    def count_pending_notifications(self):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT count(*) AS total FROM catalog_notifications"
            ).fetchone()
        return row["total"]

    def list_notifications(self):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT activity_id, kind, dedup_key, payload, run_id "
                "FROM catalog_notifications ORDER BY id"
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

    # ----- conflicts ------------------------------------------------------
    def sync_conflicts(self, conflicts, run_id):
        """以本轮投影为准持久化冲突，已解决的历史冲突标记 resolved。"""
        open_keys = {(item["id"], item["field"]) for item in conflicts}
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT activity_id, field FROM catalog_conflicts"
            ).fetchall()
            for row in rows:
                if (row["activity_id"], row["field"]) not in open_keys:
                    connection.execute(
                        "UPDATE catalog_conflicts SET resolved = 1 "
                        "WHERE activity_id = ? AND field = ?",
                        (row["activity_id"], row["field"]),
                    )
            for item in conflicts:
                connection.execute(
                    "INSERT INTO catalog_conflicts"
                    "(activity_id, field, revision, evidence, resolved, updated_run_id) "
                    "VALUES (?,?,?,?,0,?) "
                    "ON CONFLICT(activity_id, field) DO UPDATE SET "
                    "revision = excluded.revision, evidence = excluded.evidence, "
                    "resolved = 0, updated_run_id = excluded.updated_run_id",
                    (
                        item["id"],
                        item["field"],
                        item["revision"],
                        json.dumps(item["evidence"], ensure_ascii=False, sort_keys=True),
                        run_id,
                    ),
                )

    def open_conflicts(self):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT activity_id, field, revision, evidence, updated_run_id "
                "FROM catalog_conflicts WHERE resolved = 0 "
                "ORDER BY activity_id, field"
            ).fetchall()
        return [
            {
                "id": row["activity_id"],
                "field": row["field"],
                "revision": row["revision"],
                "evidence": json.loads(row["evidence"]),
                "values": sorted({e["value"] for e in json.loads(row["evidence"])}),
                "run_id": row["updated_run_id"],
            }
            for row in rows
        ]

    # ----- checkpoint -----------------------------------------------------
    def update_checkpoint(self, run_id, updated_at, scope=CATALOG_SCOPE):
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO catalog_checkpoints(scope, run_id, updated_at) "
                "VALUES (?,?,?) "
                "ON CONFLICT(scope) DO UPDATE SET run_id = excluded.run_id, "
                "updated_at = excluded.updated_at",
                (scope, run_id, updated_at),
            )

    def get_checkpoint(self, scope=CATALOG_SCOPE):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT run_id, updated_at FROM catalog_checkpoints WHERE scope = ?",
                (scope,),
            ).fetchone()
        return dict(row) if row else None
