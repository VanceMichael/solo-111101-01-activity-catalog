"""活动修订的纯函数合并规则。

稳定键为活动 id（HTML 的 data-id），绝不按标题或城市推断合并。
组织方（organizer）控制 status、venue；分类页（industry）只补 title、city。
同字段取最大 revision 的证据；同 revision 不同值则保留两份证据并进入 conflicts，
不按输入顺序挑选赢家。
"""

from collections import defaultdict

# 每个业务字段允许贡献的来源类型，以及取值后应排序的键顺序
FIELD_AUTHORITIES = {
    "title": ("industry", "organizer"),
    "city": ("industry", "organizer"),
    "status": ("organizer",),
    "venue": ("organizer",),
}
FIELD_ORDER = ("title", "city", "status", "venue")
STATUS_FIELDS = ("status", "venue")


def normalize_authority(value):
    text = str(value or "industry").strip().lower()
    return text or "industry"


def normalize_revision(value):
    if value is None or str(value).strip() == "":
        return 0
    return int(str(value).strip())


def article_to_observation(article):
    """把 adapter 解析出的一条 article data-* 转成规范化观测证据。"""
    if not isinstance(article, dict) or article.get("kind") == "pagination":
        return None
    activity_id = article.get("id")
    if not activity_id:
        return None
    fields = {}
    for field in FIELD_ORDER:
        if field in article and article[field] is not None and str(article[field]) != "":
            fields[field] = str(article[field])
    return {
        "id": str(activity_id),
        "revision": normalize_revision(article.get("revision")),
        "authority": normalize_authority(article.get("authority")),
        "fields": fields,
    }


def eligible(field, observation):
    return observation["authority"] in FIELD_AUTHORITIES[field]


def merge_event(activity_id, observations):
    """对同一活动 id 的全部历史观测做字段级合并。

    返回 (event, conflicts)。event 含合并字段（冲突字段为 None）与逐字段 provenance；
    conflicts 为该活动当前未解决的字段冲突清单。
    """
    event = {"id": activity_id, "title": None, "city": None, "status": None, "venue": None}
    provenance = {}
    conflicts = []

    for field in FIELD_ORDER:
        # 该字段的候选证据：(revision, authority, value, observation)
        candidates = []
        for observation in observations:
            if not eligible(field, observation):
                continue
            value = observation["fields"].get(field)
            if value is None:
                continue
            candidates.append(
                (
                    observation["revision"],
                    observation["authority"],
                    value,
                    observation,
                )
            )
        if not candidates:
            continue
        max_revision = max(item[0] for item in candidates)
        top = [item for item in candidates if item[0] == max_revision]
        evidence = [
            {
                "route": item[3]["route"],
                "revision": item[0],
                "authority": item[1],
                "value": item[2],
                "observed_at": item[3]["observed_at"],
                "run_id": item[3].get("run_id"),
            }
            for item in sorted(top, key=lambda item: (item[1], item[2], item[3]["route"]))
        ]
        distinct = sorted({item[2] for item in top})
        provenance[field] = {
            "revision": max_revision,
            "winning_value": distinct[0] if len(distinct) == 1 else None,
            "evidence": evidence,
        }
        if len(distinct) == 1:
            event[field] = distinct[0]
        else:
            # 同权同版字段不同：保留两份证据，不按输入顺序选赢家
            conflicts.append(
                {
                    "id": activity_id,
                    "field": field,
                    "revision": max_revision,
                    "values": distinct,
                    "evidence": evidence,
                }
            )
    event["provenance"] = provenance
    return event, conflicts


def build_projection(observations):
    """依据全部历史观测构建业务投影：events（按 id 排序）与 conflicts。"""
    grouped = defaultdict(list)
    for observation in observations:
        grouped[observation["id"]].append(observation)
    events = []
    conflicts = []
    for activity_id in sorted(grouped):
        event, event_conflicts = merge_event(activity_id, grouped[activity_id])
        events.append(event)
        conflicts.extend(event_conflicts)
    conflicts.sort(key=lambda item: (item["id"], item["field"]))
    return events, conflicts


def apply_absence(events, present_ids):
    """全量完成后标记缺席活动；缺席不是删除，活动记录仍保留。"""
    for event in events:
        event["absent"] = event["id"] not in present_ids
    return events
