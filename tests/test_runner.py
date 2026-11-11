import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from collector_01.catalog_store import CatalogStore
from collector_01.clock import Clock
from collector_01.runner import load_manifest, parse_retry_after, run_scenario


def write_fixture(root, routes, files):
    (root / "routes.json").write_text(json.dumps(routes, ensure_ascii=False))
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8")


def scenario(root, run_id, sources, budget, mode="full", portal=None):
    return {
        "name": run_id,
        "run_id": run_id,
        "sources": sources,
        "options": {"mode": mode, "request_budget": budget},
        "fixture_root": str(root),
        "portal_database": portal,
    }


class EngineDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "fixtures"
        self.root.mkdir()
        self.state = Path(self.tmp.name) / "state.sqlite3"
        self.output = Path(self.tmp.name) / "result.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, manifest, clock=None):
        return run_scenario(manifest, self.state, self.output, clock=clock)

    def test_root_page_recursively_discovers_pagination_chain(self):
        write_fixture(
            self.root,
            {
                "/root": [{"status": 200, "file": "p1.html"}],
                "/p2": [{"status": 200, "file": "p2.html"}],
                "/p3": [{"status": 200, "file": "p3.html"}],
            },
            {
                "p1.html": '<article data-id="a" data-revision="1" data-authority="industry" data-title="A" data-city="杭州"></article>'
                '<a rel="next" href="/p2"></a>',
                "p2.html": '<article data-id="b" data-revision="1" data-authority="organizer" data-status="open" data-venue="B馆"></article>'
                '<a rel="next" href="/p3"></a>',
                "p3.html": '<article data-id="c" data-revision="1" data-authority="organizer" data-status="open"></article>',
            },
        )
        result = self._run(scenario(self.root, "r1", ["/root"], budget=3))
        self.assertEqual(result["coverage"], "complete")
        self.assertEqual(result["checkpoint"], "r1")
        self.assertIsNone(result["retry_after"])
        self.assertEqual([e["id"] for e in result["events"]], ["a", "b", "c"])
        self.assertEqual([r["route"] for r in result["requests"]], ["/root", "/p2", "/p3"])

    def test_pagination_loop_does_not_revisit_or_hang(self):
        write_fixture(
            self.root,
            {
                "/root": [{"status": 200, "file": "p1.html"}],
                "/p2": [{"status": 200, "file": "p2.html"}],
            },
            {
                # 两个页面互相 next，构成环
                "p1.html": '<a rel="next" href="/p2"></a>',
                "p2.html": '<a rel="next" href="/root"></a>',
            },
        )
        result = self._run(scenario(self.root, "r1", ["/root"], budget=5))
        self.assertEqual(result["coverage"], "complete")
        self.assertEqual([r["route"] for r in result["requests"]], ["/root", "/p2"])

    def test_failed_request_consumes_budget_and_returns_partial_without_checkpoint(self):
        write_fixture(
            self.root,
            {
                "/root": [{"status": 200, "file": "p1.html"}],
                "/p2": [{"status": 503}],
            },
            {"p1.html": '<a rel="next" href="/p2"></a>'},
        )
        result = self._run(scenario(self.root, "r1", ["/root"], budget=3))
        self.assertEqual(result["coverage"], "partial")
        self.assertIsNone(result["checkpoint"])
        self.assertIsNone(result["retry_after"])
        self.assertEqual(result["failure"], "http_503")
        self.assertEqual(result["budget"]["used"], 2)

    def test_rate_limit_uses_injected_clock_and_budget_shortfall_returns_retry_after(self):
        write_fixture(
            self.root,
            {
                "/root": [{"status": 200, "file": "p1.html"}],
                "/p2": [
                    {"status": 429, "headers": {"Retry-After": "2"}},
                    {"status": 200, "file": "p2.html"},
                ],
            },
            {
                "p1.html": '<article data-id="a" data-revision="1" data-authority="organizer" data-status="open"></article>'
                '<a rel="next" href="/p2"></a>',
                "p2.html": '<article data-id="b" data-revision="1" data-authority="organizer" data-status="open"></article>',
            },
        )
        # 预算只够第一次 429：立即 partial 且带 retry_after
        clock = Clock()
        short = self._run(scenario(self.root, "short", ["/root"], budget=2), clock=clock)
        self.assertEqual(short["coverage"], "partial")
        self.assertEqual(short["retry_after"], 2)
        self.assertIsNone(short["checkpoint"])
        self.assertEqual(short["failure"], "rate_limited")

        # 新进程、新时钟、预算充足：逻辑时钟推进 2 秒后重试成功，无真实 sleep
        clock2 = Clock()
        full = self._run(scenario(self.root, "full", ["/root"], budget=3), clock=clock2)
        self.assertEqual(full["coverage"], "complete")
        self.assertIsNone(full["retry_after"])
        request_rows = full["requests"]
        self.assertEqual([r["status_code"] for r in request_rows], [200, 429, 200])
        self.assertEqual(request_rows[2]["retry_after"], None)
        self.assertEqual(clock2.now(), 2.0)

    def test_parse_retry_after_supports_seconds_and_http_date(self):
        clock = Clock()
        self.assertEqual(parse_retry_after("5", clock), 5)
        from email.utils import formatdate
        from datetime import datetime, timezone

        target = clock.epoch.timestamp() + 30
        header = formatdate(target, usegmt=True)
        # HTTP-date 只有秒级精度，允许取整误差
        self.assertIn(parse_retry_after(header, clock), (29, 30))

    def test_partial_run_does_not_mark_absent_and_full_run_then_detects_absence(self):
        write_fixture(
            self.root,
            {
                "/root": [{"status": 200, "file": "all.html"}],
                "/late": [
                    {"status": 429, "headers": {"Retry-After": "1"}},
                    {"status": 200, "file": "all.html"},
                ],
            },
            {
                "all.html": '<article data-id="a" data-revision="1" data-authority="organizer" data-status="open"></article>'
                '<article data-id="b" data-revision="1" data-authority="organizer" data-status="open"></article>'
            },
        )
        # 先完整抓到 a、b
        first = self._run(scenario(self.root, "r1", ["/root"], budget=1))
        self.assertEqual(first["coverage"], "complete")
        self.assertEqual(len(first["events"]), 2)

        # 页面变更：第二轮全量只返回 a（b 缺席），但先遇到限流预算耗尽 -> partial
        (self.root / "all.html").write_text(
            '<article data-id="a" data-revision="1" data-authority="organizer" data-status="open"></article>',
            encoding="utf-8",
        )
        partial = self._run(scenario(self.root, "r2", ["/late"], budget=1))
        self.assertEqual(partial["coverage"], "partial")
        # partial 不能误报新版本：checkpoint 仍指向上一轮完整 run
        self.assertEqual(partial["checkpoint"], "r1")
        # b 没有被删除或缺席
        by_id = {e["id"]: e for e in partial["events"]}
        self.assertIn("b", by_id)
        self.assertFalse(by_id["b"]["absent"])

        # 预算充足后重新开进程完成全量，才能判定 b 缺席
        complete = self._run(scenario(self.root, "r3", ["/late"], budget=2))
        self.assertEqual(complete["coverage"], "complete")
        self.assertEqual(complete["checkpoint"], "r3")
        by_id = {e["id"]: e for e in complete["events"]}
        self.assertIn("b", by_id)  # 缺席是标记，不是删除
        self.assertTrue(by_id["b"]["absent"])
        self.assertFalse(by_id["a"]["absent"])

    def test_incremental_never_deletes_missing_events(self):
        write_fixture(
            self.root,
            {
                "/root": [{"status": 200, "file": "p1.html"}],
            },
            {
                "p1.html": '<article data-id="a" data-revision="1" data-authority="organizer" data-status="open"></article>'
                '<article data-id="b" data-revision="1" data-authority="organizer" data-status="open"></article>'
            },
        )
        self._run(scenario(self.root, "r1", ["/root"], budget=1, mode="full"))
        # 增量只出现 a，b 未出现也不得缺席/删除
        (self.root / "p1.html").write_text(
            '<article data-id="a" data-revision="2" data-authority="organizer" data-status="open"></article>',
            encoding="utf-8",
        )
        result = self._run(scenario(self.root, "r2", ["/root"], budget=1, mode="incremental"))
        self.assertEqual(result["coverage"], "complete")
        by_id = {e["id"]: e for e in result["events"]}
        self.assertEqual(set(by_id), {"a", "b"})
        self.assertFalse(by_id["b"]["absent"])

    def test_page_change_creates_business_revision_but_identical_reimport_does_not(self):
        write_fixture(
            self.root,
            {"/root": [{"status": 200, "file": "p.html"}]},
            {
                "p.html": '<article data-id="a" data-revision="1" data-authority="organizer" data-status="open" data-venue="A馆"></article>'
            },
        )
        self._run(scenario(self.root, "r1", ["/root"], budget=1))
        # 同进程内重放（模拟新进程共享 database）：无新增业务版本
        again = self._run(scenario(self.root, "r1", ["/root"], budget=1))
        self.assertEqual(again["notifications"], 0)
        store = CatalogStore(self.state)
        with store.connect() as conn:
            count = conn.execute(
                "SELECT count(*) AS n FROM catalog_observations"
            ).fetchone()["n"]
        self.assertEqual(count, 1)

        # 页面变更：revision 提高、字段变化，产生新业务版本与状态通知
        (self.root / "p.html").write_text(
            '<article data-id="a" data-revision="2" data-authority="organizer" data-status="cancelled" data-venue="A馆"></article>',
            encoding="utf-8",
        )
        changed = self._run(scenario(self.root, "r2", ["/root"], budget=1))
        self.assertEqual(changed["notifications"], 1)
        event = changed["events"][0]
        self.assertEqual(event["status"], "cancelled")
        self.assertEqual(event["provenance"]["status"]["revision"], 2)
        with store.connect() as conn:
            count = conn.execute(
                "SELECT count(*) AS n FROM catalog_observations"
            ).fetchone()["n"]
        self.assertEqual(count, 2)
        # 再重放新版本仍然幂等
        replay = self._run(scenario(self.root, "r2", ["/root"], budget=1))
        self.assertEqual(replay["notifications"], 0)
        with store.connect() as conn:
            count = conn.execute(
                "SELECT count(*) AS n FROM catalog_observations"
            ).fetchone()["n"]
        self.assertEqual(count, 2)

    def test_conflict_persists_and_resolves_when_evidence_converges(self):
        write_fixture(
            self.root,
            {
                "/r1": [{"status": 200, "file": "a.html"}],
                "/r2": [{"status": 200, "file": "b.html"}],
            },
            {
                "a.html": '<article data-id="a" data-revision="4" data-authority="organizer" data-status="open" data-venue="B馆"></article>',
                "b.html": '<article data-id="a" data-revision="4" data-authority="organizer" data-status="open" data-venue="C馆"></article>',
            },
        )
        result = self._run(scenario(self.root, "r1", ["/r1", "/r2"], budget=2, mode="incremental"))
        self.assertEqual(len(result["conflicts"]), 1)
        store = CatalogStore(self.state)
        self.assertEqual(len(store.open_conflicts()), 1)

        # 其中一页给出更大 revision 的收敛值，冲突解决（保留历史行）
        write_fixture(
            self.root,
            {
                "/r1": [{"status": 200, "file": "a2.html"}],
                "/r2": [{"status": 200, "file": "b.html"}],
            },
            {
                "a2.html": '<article data-id="a" data-revision="5" data-authority="organizer" data-status="open" data-venue="D馆"></article>',
                "b.html": '<article data-id="a" data-revision="4" data-authority="organizer" data-status="open" data-venue="C馆"></article>',
            },
        )
        resolved = self._run(scenario(self.root, "r2", ["/r1", "/r2"], budget=2, mode="incremental"))
        self.assertEqual(resolved["conflicts"], [])
        event = resolved["events"][0]
        self.assertEqual(event["venue"], "D馆")
        self.assertEqual(len(store.open_conflicts()), 0)

    def test_portal_database_receives_projection_and_pending_notifications(self):
        portal = Path(self.tmp.name) / "portal.sqlite3"
        write_fixture(
            self.root,
            {"/root": [{"status": 200, "file": "p.html"}]},
            {
                "p.html": '<article data-id="a" data-revision="1" data-authority="organizer" data-status="open" data-venue="A馆"></article>'
            },
        )
        self._run(scenario(self.root, "r1", ["/root"], budget=1, portal=str(portal)))
        with sqlite3.connect(portal) as conn:
            rows = conn.execute("SELECT activity_id, status, venue FROM portal_events").fetchall()
            notifications = conn.execute(
                "SELECT activity_id, kind FROM portal_notifications"
            ).fetchall()
        self.assertEqual(rows, [("a", "open", "A馆")])
        self.assertEqual(notifications, [("a", "created")])

    def test_manifest_validation(self):
        with self.assertRaisesRegex(ValueError, "run_id"):
            load_manifest({"sources": ["/r"], "options": {"mode": "full", "request_budget": 1}, "fixture_root": "."})
        with self.assertRaisesRegex(ValueError, "mode"):
            load_manifest({"run_id": "x", "sources": ["/r"], "options": {"mode": "weird", "request_budget": 1}, "fixture_root": "."})
        with self.assertRaisesRegex(ValueError, "request_budget"):
            load_manifest({"run_id": "x", "sources": ["/r"], "options": {"mode": "full", "request_budget": 0}, "fixture_root": "."})


if __name__ == "__main__":
    unittest.main()
