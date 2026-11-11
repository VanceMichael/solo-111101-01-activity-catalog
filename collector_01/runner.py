"""场景化全量/增量采集引擎。

发现：由根来源 BFS 递归，跟随 HTML 的 rel=next 分页线索，已访问路由去重防环。
预算：每次 request（含 429 与其它失败、重试）都计入 request_budget。
限流：429 按 Retry-After 通过可注入逻辑时钟安排重试，不做真实 sleep；
预算不足立即返回 partial 与 retry_after。
完整性：只有全部发现页面成功才是 complete，才更新 checkpoint；全量模式此时
才可判定缺席活动。增量模式永不删除未出现的活动。
"""

import json
from collections import deque
from pathlib import Path

from .adapter import decode
from .catalog_store import CATALOG_SCOPE, CatalogStore
from .clock import Clock
from .merge import apply_absence, article_to_observation, build_projection
from .portal import PortalStore
from .sources import FixtureTransport

VALID_MODES = ("full", "incremental")


def parse_retry_after(value, clock):
    """支持秒数与 HTTP-date 两种 Retry-After，统一换算成逻辑秒。"""
    if value is None:
        return 0
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    from email.utils import parsedate_to_datetime

    target = parsedate_to_datetime(text)
    if target is None:
        return 0
    base = clock.epoch
    delta = (target - base).total_seconds() - clock.now()
    return max(0, int(round(delta)))


def load_manifest(manifest):
    if isinstance(manifest, (str, Path)):
        data = json.loads(Path(manifest).read_text())
    else:
        data = dict(manifest)
    run_id = str(data.get("run_id", "")).strip()
    sources = data.get("sources")
    options = data.get("options") or {}
    mode = options.get("mode")
    budget = options.get("request_budget")
    fixture_root = data.get("fixture_root")
    if not run_id:
        raise ValueError("场景缺少 run_id")
    if not isinstance(sources, list) or not sources:
        raise ValueError("场景 sources 必须是非空数组")
    if mode not in VALID_MODES:
        raise ValueError(f"options.mode 必须是 {VALID_MODES} 之一")
    if not isinstance(budget, int) or budget <= 0:
        raise ValueError("options.request_budget 必须是正整数")
    if not fixture_root:
        raise ValueError("场景缺少 fixture_root")
    return {
        "name": data.get("name") or run_id,
        "run_id": run_id,
        "sources": [str(route) for route in sources],
        "mode": mode,
        "request_budget": budget,
        "fixture_root": str(fixture_root),
        "portal_database": data.get("portal_database"),
    }


def run_scenario(manifest, database, output, clock=None, transport_factory=None):
    scenario = load_manifest(manifest)
    clock = clock or Clock()
    transport_factory = transport_factory or FixtureTransport
    transport = transport_factory(scenario["fixture_root"])
    store = CatalogStore(database)
    store.initialize()

    run_id = scenario["run_id"]
    mode = scenario["mode"]
    budget_total = scenario["request_budget"]
    started_at = clock.now_iso()

    # 运行前快照：用于区分新建活动与状态修订（恢复/取消）
    prior_events, _ = build_projection(store.load_observations())
    prior_status = {event["id"]: event.get("status") for event in prior_events}
    prior_ids = set(prior_status)

    attempt = store.begin_run(
        run_id, scenario["name"], mode, budget_total, started_at, scenario["sources"]
    )

    queue = deque(scenario["sources"])
    visited = set()
    discovered = set(scenario["sources"])
    budget_used = 0
    seq = 0
    coverage = "complete"
    retry_after = None
    failure = None

    while queue:
        route = queue.popleft()
        if route in visited:
            continue
        if budget_used >= budget_total:
            coverage = "partial"
            failure = "budget_exhausted"
            break

        # 单路由重试循环；429 与重试都各自消耗预算
        while True:
            scheduled_at = clock.now_iso()
            response = transport.request(route)
            budget_used += 1
            seq += 1
            observed_at = clock.now_iso()
            request_retry = None
            if response.status == 429:
                request_retry = parse_retry_after(
                    response.headers.get("Retry-After"), clock
                )
            store.record_request(
                run_id,
                attempt,
                seq,
                route,
                response.status,
                scheduled_at,
                observed_at if response.status == 200 else None,
                request_retry,
            )

            if response.status == 429:
                retry_after = request_retry
                if budget_used >= budget_total:
                    coverage = "partial"
                    failure = "rate_limited"
                    break
                # 逻辑时钟推进到允许重试的时刻，不做真实 sleep
                clock.advance(request_retry)
                continue
            if response.status != 200:
                coverage = "partial"
                retry_after = None
                failure = f"http_{response.status}"
                break

            retry_after = None
            for item in decode(response.body):
                if item.get("kind") == "pagination":
                    next_route = item.get("next")
                    if next_route and next_route not in discovered:
                        discovered.add(next_route)
                        queue.append(next_route)
                    continue
                observation = article_to_observation(item)
                if observation is None:
                    continue
                store.insert_observation(run_id, observation, route, observed_at)
            visited.add(route)
            break

        if coverage == "partial":
            break

    finished_at = clock.now_iso()
    store.finish_run(run_id, attempt, coverage, retry_after, budget_used, finished_at)

    # 业务投影基于全部历史观测；部分抓取不产生删除或缺席判定
    events, conflicts = build_projection(store.load_observations())
    present_ids = store.run_present_ids(run_id)
    present_set = set(present_ids)

    notifications_created = 0
    if coverage == "complete":
        # 确保所有已知活动都有生命周期行，再按本轮出现集合对齐缺席标记
        for event in events:
            store.upsert_event(event["id"], run_id, False, finished_at)
        if mode == "full":
            store.mark_absent(present_ids, run_id, finished_at)
        store.update_checkpoint(run_id, finished_at)
        checkpoint = run_id
    else:
        checkpoint_row = store.get_checkpoint(CATALOG_SCOPE)
        checkpoint = checkpoint_row["run_id"] if checkpoint_row else None
        # 部分抓取只登记本轮出现的活动，绝不改动其它活动的缺席标记
        for activity_id in present_ids:
            store.upsert_event(activity_id, run_id, False, finished_at)

    for event in events:
        lifecycle = store.get_event(event["id"])
        event["absent"] = bool(lifecycle and lifecycle["absent"])

    store.sync_conflicts(conflicts, run_id)

    # 通知：新建活动与状态修订（取消/恢复），按业务键幂等
    for event in events:
        activity_id = event["id"]
        if activity_id not in prior_ids:
            created = store.add_notification(
                activity_id,
                "created",
                f"created:{activity_id}",
                {"id": activity_id, "run_id": run_id},
                run_id,
            )
            notifications_created += int(created)
            continue
        current_status = event.get("status")
        if current_status is not None and current_status != prior_status.get(activity_id):
            status_revision = event["provenance"]["status"]["revision"]
            created = store.add_notification(
                activity_id,
                "status_changed",
                f"status:{activity_id}:{status_revision}:{current_status}",
                {
                    "id": activity_id,
                    "status": current_status,
                    "revision": status_revision,
                    "run_id": run_id,
                },
                run_id,
            )
            notifications_created += int(created)

    # 门户库投影（与采集状态库分离，供运营侧使用）
    portal_notifications = []
    portal_path = scenario.get("portal_database")
    if portal_path:
        portal = PortalStore(portal_path)
        portal.initialize()
        portal.replace_events(events, run_id, finished_at)
        for row in store.list_notifications():
            portal.add_notification(
                row["id"],
                row["kind"],
                row["dedup_key"],
                row["payload"],
                row["run_id"],
                finished_at,
            )
        portal_notifications = portal.list_notifications()

    result = {
        "run_id": run_id,
        "name": scenario["name"],
        "mode": mode,
        "coverage": coverage,
        "checkpoint": checkpoint,
        "retry_after": retry_after if coverage == "partial" else None,
        "events": events,
        "conflicts": conflicts,
        "notifications": notifications_created,
        "pending_notifications": portal_notifications,
        "budget": {"total": budget_total, "used": budget_used},
        "requests": store.fetch_requests(run_id, attempt),
        "failure": failure,
    }
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result
