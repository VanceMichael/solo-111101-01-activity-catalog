import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CliWorkflowTests(unittest.TestCase):
    def test_run_command_writes_business_projection_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            manifest = work / "scenario.json"
            manifest.write_text(
                json.dumps(
                    {
                        "name": "incomplete",
                        "run_id": "run-incomplete",
                        "sources": ["/industry/1"],
                        "options": {"mode": "full", "request_budget": 2},
                        "fixture_root": str(ROOT / "fixtures"),
                        "portal_database": str(work / "portal.sqlite3"),
                    },
                    ensure_ascii=False,
                )
            )
            database = work / "state.sqlite3"
            output = work / "result.json"
            command = [
                sys.executable,
                "-m",
                "collector_01.cli",
                "--run",
                str(manifest),
                "--database",
                str(database),
                "--output",
                str(output),
            ]
            first = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=5)
            self.assertEqual(first.returncode, 0, first.stderr)
            result = json.loads(output.read_text())
            self.assertEqual(result["coverage"], "partial")
            self.assertIsNone(result["checkpoint"])
            self.assertEqual(result["retry_after"], 2)
            self.assertEqual(result["events"][0]["id"], "fair-7")

            # 重新开进程重放：同一状态库，投影一致且无新通知
            second = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=5)
            self.assertEqual(second.returncode, 0, second.stderr)
            repeated = json.loads(output.read_text())
            self.assertEqual(repeated["coverage"], "partial")
            self.assertEqual(repeated["retry_after"], 2)
            self.assertEqual(repeated["notifications"], 0)
            self.assertEqual(
                [e["id"] for e in repeated["events"]],
                [e["id"] for e in result["events"]],
            )

    def test_run_requires_output_argument(self):
        run = subprocess.run(
            [sys.executable, "-m", "collector_01.cli", "--run", "x.json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("--output", run.stderr)


if __name__ == "__main__":
    unittest.main()
