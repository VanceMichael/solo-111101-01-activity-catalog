# 活动目录采集的跨页修订合并

离线采集工程使用 Python 标准库和 SQLite。adapter 读取本项目的真实来源格式，sources 提供可重复响应序列，capture 原子归档解析后的来源记录。`--capture` 只记录原始观察，不作业务投影；`--run` 执行场景化全量/增量采集，按活动稳定身份合并跨页修订并输出业务投影。

## 环境与准备

推荐 Python 3.12.12，支持 Python 3.11+；requirements.lock 为空表示没有第三方包，运行、测试和验收均不下载数据或依赖。Dockerfile 提供同一 Linux 环境。需要容器时先在正式运行之外准备：

```sh
docker build -t q001-activity-catalog:initial .
docker run --rm q001-activity-catalog:initial
```

## 基线测试与原始采集

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q collector_01 tests acceptance
scratch=$(mktemp -d)
python3 -m collector_01.cli --database "$scratch/raw.sqlite3" --capture fixtures --route /industry/1
python3 -m collector_01.cli --database "$scratch/raw.sqlite3" --capture fixtures --route /industry/1
```

第一次 decoded=2、inserted=2，重抓同一观测 inserted=0。临时库不提交。tests 是已实现采集基础能力的回归测试，不代表扩展功能完成。

## 来源协议

HTML 的 article data-* 是站点字段，rel=next 是分页线索。稳定键为活动 id，不用标题或城市推断合并。组织方控制 status、venue；分类页只补 title、city。较大 revision 生效，同权同版字段不同则保留两份证据并进入 conflicts，不能按输入顺序选赢家。取消是状态修订不是删除，恢复只接受更新组织方版本。每条合并字段要能在 provenance 中查到 route、revision、原值。

全量由根页递归发现分页，request_budget 计入失败请求；429 按 Retry-After 使用可注入逻辑时钟安排重试，预算不足时立即返回 partial 和 retry_after。只有全部发现页面成功才更新 checkpoint 并可判定缺席活动；增量不能把未出现活动删除。多次进程执行共享 database 后重复导入不新增业务版本。

FixtureTransport 每次进程创建从 routes.json 首条响应开始，同一路由多次 request 依次推进，最后一条可重复；calls 保存请求证据。源数据相对 fixture_root 读取，不能越出该目录。所有响应是本地合成数据，不发出公网请求。单场景 JSON 的 fixture_root 相对当前工作目录解析；在仓库根目录执行即可。run_id 是采集批次标识，name 仅作展示标签。

## 场景文件格式

`--run` 的场景文件为单个 JSON 对象（仓库 fixtures/*.json 即示例，由验收脚本按同一格式生成）：

```json
{
  "name": "incomplete",
  "run_id": "run-incomplete",
  "sources": ["/industry/1"],
  "options": {"mode": "full", "request_budget": 2},
  "fixture_root": "fixtures",
  "portal_database": "portal.sqlite3"
}
```

- `run_id`：采集批次标识；同一 run_id 被新进程再次执行视为重放，不新增业务版本。
- `sources`：有序根来源路由，采集器从根页 BFS 跟随 `rel=next` 递归发现分页，已访问路由去重防环。
- `options.mode`：`full`（全量）或 `incremental`（增量）。
- `options.request_budget`：正整数；每次 request（含 429、其它失败与重试）都计入预算。
- `fixture_root`：routes.json 与 HTML 所在目录，相对当前工作目录解析。
- `portal_database`：可选，运营侧门户库；采集状态库与门户库分离。

## 扩展命令与业务输出

```sh
python3 -m collector_01.cli --run fixtures/incomplete.json \
  --database collector_01.sqlite3 --output incomplete.json
```

输出 JSON 的业务投影（events 按 id 排序）：

- `coverage`：`complete` 仅当全部发现页面成功；任何失败、限流未恢复或预算耗尽均为 `partial`。
- `checkpoint`：完整时为本轮 run_id；partial 时保留上一轮完整 checkpoint（没有则为 null）。
- `retry_after`：partial 且最后原因为 429 时为 Retry-After 秒数；无需重试时为 null。
- `events`：当前活动记录。每个事件含合并字段 `title/city/status/venue`（冲突字段为 null）、`absent`（仅全量完成后可标记，缺席不删除记录）与逐字段 `provenance`。
- `provenance.<字段>`：含 `revision`、`winning_value` 与 `evidence` 列表；每条证据可查到 `route`、`revision`、`authority`、原始 `value`、`observed_at`、`run_id`。
- `conflicts`：同 revision 不同值的未解决字段冲突，含 `id`、`field`、`revision`、`values` 与双方证据；冲突字段不取赢家。更大 revision 收敛后冲突自动标记解决。
- `notifications`：本次新建通知数（活动新建、状态修订如取消/恢复），按业务键幂等，重放为 0；`pending_notifications` 为门户库中的待处理通知清单。
- `budget`、`requests`、`failure`：预算消耗、逐次请求证据（含状态码、调度与观测时间、Retry-After）与中断原因，供运营追溯运行状态。

### 合并与状态规则

- 分类页（industry）的 status、venue 一律不参与合并；组织方（organizer）可补 title、city。
- 同字段取最大 revision 的证据；输入顺序不影响结果。revision 相同且取值不同即冲突，两份证据都保留。
- 取消产生更大 revision 的 status 证据，记录仍保留；恢复只接受更新的组织方 revision，旧版本不能撤销当前取消。
- partial 运行已抓到的证据照常入库，但不更新 checkpoint、不判定缺席、不删除任何活动。
- 增量模式下未出现的活动保持原状，绝不删除或标记缺席。
- 观测指纹只由路由与站点字段内容决定：跨进程重复导入相同页面不产生新业务版本；页面字段变更才产生新版本。每次请求的完整时间线保存在请求证据表中。

## 状态库与门户库

采集状态库（`--database`）内部表（实现细节，可随版本演进）：`catalog_runs`（批次与每次进程尝试）、`catalog_requests`（请求证据）、`catalog_observations`（去重后的活动观测证据）、`catalog_events`（活动生命周期与缺席标记）、`catalog_notifications`（幂等通知）、`catalog_conflicts`（冲突及解决标记）、`catalog_checkpoints`。原始 `source_records` 表保持不变，与业务投影互不影响。`portal_database` 另存 `portal_events` 全量投影与 `portal_notifications` 待处理清单。

## 验收与正式展示

```sh
python3 acceptance/check.py --contracts-only
python3 acceptance/check.py
```

contracts-only 仅检查材料结构，不能当成功能验收。四个场景（incomplete 限流 partial、catalog 全量合并、restore 取消恢复、conflict 同版字段冲突）每个在独立临时目录用两个独立进程各执行一次，检查重启与重复输入；第二次进程新建通知数为 0、业务投影保持一致。回归测试另覆盖根页递归发现、分页环、预算与失败请求、429 时钟重试、页面变更产生新版本、缺席判定、增量不删除、冲突收敛与 CLI 端到端。正式展示从干净状态执行基线测试和第二条命令；不修改预期来迁就实现。
