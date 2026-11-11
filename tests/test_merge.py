import unittest

from collector_01.merge import (
    apply_absence,
    article_to_observation,
    build_projection,
    merge_event,
    normalize_authority,
    normalize_revision,
)


def obs(activity_id, revision, authority, route="/r", observed_at="2026-11-11T10:00:00Z", **fields):
    return {
        "id": activity_id,
        "revision": revision,
        "authority": authority,
        "route": route,
        "observed_at": observed_at,
        "fields": fields,
    }


class ArticleParsingTests(unittest.TestCase):
    def test_article_to_observation_ignores_pagination_and_missing_id(self):
        self.assertIsNone(article_to_observation({"kind": "pagination", "next": "/p"}))
        self.assertIsNone(article_to_observation({"revision": "1"}))
        parsed = article_to_observation(
            {
                "id": "a1",
                "revision": "5",
                "authority": "organizer",
                "status": "open",
                "venue": "A馆",
                "title": "校招",
                "city": "杭州",
            }
        )
        self.assertEqual(parsed["id"], "a1")
        self.assertEqual(parsed["revision"], 5)
        self.assertEqual(parsed["authority"], "organizer")
        self.assertEqual(
            parsed["fields"],
            {"status": "open", "venue": "A馆", "title": "校招", "city": "杭州"},
        )

    def test_defaults_for_missing_revision_and_authority(self):
        parsed = article_to_observation({"id": "a1", "title": "x"})
        self.assertEqual(parsed["revision"], 0)
        self.assertEqual(normalize_authority(None), "industry")
        self.assertEqual(normalize_revision(""), 0)
        self.assertEqual(normalize_revision("7"), 7)
        with self.assertRaises(ValueError):
            normalize_revision("not-a-number")


class MergeRuleTests(unittest.TestCase):
    def test_industry_page_cannot_overwrite_organizer_status_or_venue(self):
        observations = [
            obs("a", 1, "organizer", "/org", status="open", venue="A馆"),
            obs("a", 9, "industry", "/cat", status="cancelled", venue="X馆",
                title="校招", city="杭州"),
        ]
        event, conflicts = merge_event("a", observations)
        self.assertEqual(event["status"], "open")
        self.assertEqual(event["venue"], "A馆")
        self.assertEqual(event["title"], "校招")
        self.assertEqual(event["city"], "杭州")
        self.assertEqual(conflicts, [])
        # 组织方字段的 provenance 不能混入分类页证据
        self.assertEqual(
            [e["route"] for e in event["provenance"]["status"]["evidence"]], ["/org"]
        )

    def test_higher_revision_wins_regardless_of_input_order(self):
        # 旧版本在输入顺序上后出现，也不能覆盖较大 revision
        observations = [
            obs("a", 3, "organizer", "/org-new", status="cancelled", venue="B馆"),
            obs("a", 2, "organizer", "/org-old", status="open", venue="A馆"),
        ]
        event, conflicts = merge_event("a", observations)
        self.assertEqual(event["status"], "cancelled")
        self.assertEqual(event["venue"], "B馆")
        self.assertEqual(event["provenance"]["status"]["revision"], 3)
        self.assertEqual(event["provenance"]["venue"]["winning_value"], "B馆")

    def test_equal_revision_conflicting_values_keep_both_evidence(self):
        observations = [
            obs("a", 4, "organizer", "/r1", status="open", venue="B馆"),
            obs("a", 4, "organizer", "/r2", status="open", venue="C馆"),
        ]
        event, conflicts = merge_event("a", observations)
        self.assertIsNone(event["venue"])
        self.assertEqual(len(conflicts), 1)
        conflict = conflicts[0]
        self.assertEqual(conflict["id"], "a")
        self.assertEqual(conflict["field"], "venue")
        self.assertEqual(conflict["revision"], 4)
        self.assertEqual(conflict["values"], ["B馆", "C馆"])
        routes = {e["route"] for e in conflict["evidence"]}
        self.assertEqual(routes, {"/r1", "/r2"})
        # 冲突不影响其它字段
        self.assertEqual(event["status"], "open")

    def test_cancel_is_status_revision_and_restore_needs_newer_organizer(self):
        # revision 2 取消，revision 4 恢复：恢复接受更新的组织方版本
        observations = [
            obs("a", 2, "organizer", "/org", status="cancelled", venue="A馆"),
            obs("a", 4, "organizer", "/org2", status="open", venue="B馆"),
        ]
        event, _ = merge_event("a", observations)
        self.assertEqual(event["status"], "open")
        self.assertEqual(event["venue"], "B馆")

        # 更旧的组织方“恢复”不能撤销当前取消状态
        blocked, _ = merge_event(
            "a",
            [
                obs("a", 3, "organizer", "/org", status="cancelled", venue="A馆"),
                obs("a", 2, "organizer", "/old", status="open", venue="B馆"),
            ],
        )
        self.assertEqual(blocked["status"], "cancelled")

    def test_provenance_carries_route_revision_and_original_value(self):
        observations = [
            obs("a", 2, "industry", "/cat", title="旧标题", city="杭州"),
            obs("a", 5, "organizer", "/org", title="新标题", city="杭州",
                status="open", venue="A馆"),
        ]
        event, _ = merge_event("a", observations)
        title_prov = event["provenance"]["title"]
        self.assertEqual(title_prov["revision"], 5)
        self.assertEqual(title_prov["winning_value"], "新标题")
        evidence = title_prov["evidence"][0]
        self.assertEqual(evidence["route"], "/org")
        self.assertEqual(evidence["revision"], 5)
        self.assertEqual(evidence["value"], "新标题")
        self.assertEqual(evidence["observed_at"], "2026-11-11T10:00:00Z")

    def test_projection_sorts_events_by_id_and_marks_absence_without_deleting(self):
        observations = [
            obs("z9", 1, "organizer", "/o", status="open"),
            obs("a1", 1, "industry", "/c", title="T", city="杭州"),
        ]
        events, conflicts = build_projection(observations)
        self.assertEqual([e["id"] for e in events], ["a1", "z9"])
        self.assertEqual(conflicts, [])
        apply_absence(events, {"z9"})
        by_id = {e["id"]: e for e in events}
        self.assertTrue(by_id["a1"]["absent"])
        self.assertFalse(by_id["z9"]["absent"])
        # 缺席活动记录仍然存在，字段不被删除
        self.assertEqual(by_id["a1"]["title"], "T")


if __name__ == "__main__":
    unittest.main()
