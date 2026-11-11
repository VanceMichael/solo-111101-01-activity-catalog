import argparse
import json
from .store import RecordStore
from .capture import capture
from .runner import run_scenario


def main(argv=None):
    parser = argparse.ArgumentParser(description='活动目录采集的跨页修订合并')
    parser.add_argument("--database", default="collector.sqlite3")
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--capture", metavar="FIXTURE_ROOT")
    parser.add_argument("--route", default='/industry/1')
    parser.add_argument("--observed-at", default="2026-11-11T10:00:00Z")
    parser.add_argument("--run", metavar="SCENARIO")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    if args.capture:
        # 保留原始采集入口：仅归档解析后的来源观测，不做业务投影
        print(json.dumps(capture(args.capture, args.route, args.database, args.observed_at), ensure_ascii=False))
        return 0
    if args.run:
        if not args.output:
            parser.error("--run 需要同时提供 --output")
        result = run_scenario(args.run, args.database, args.output)
        print(json.dumps(
            {
                "run_id": result["run_id"],
                "coverage": result["coverage"],
                "checkpoint": result["checkpoint"],
                "retry_after": result["retry_after"],
                "events": len(result["events"]),
                "conflicts": len(result["conflicts"]),
                "notifications": result["notifications"],
            },
            ensure_ascii=False,
        ))
        return 0
    if args.init:
        store = RecordStore(args.database)
        store.initialize()
        print(json.dumps({"stored": store.count()}))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
