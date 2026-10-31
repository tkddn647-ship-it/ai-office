"""키 없이 가짜 AI로 전체 흐름을 시험한다.  실행: cd desktop && python -m unittest -v"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aio_desktop import providers                      # noqa: E402
from aio_desktop.orchestrator import Run               # noqa: E402
from aio_desktop.workspace import Workspace, WorkspaceError  # noqa: E402


def git(root, *a):
    return subprocess.run(["git", *a], cwd=root, capture_output=True, text=True)


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.root = Path(self.d.name)
        (self.root / "a.txt").write_text("hello\nworld\n", encoding="utf-8")
        self.ws = Workspace(str(self.root))

    def tearDown(self):
        self.d.cleanup()

    def test_blocks_outside_paths(self):
        for bad in ["../x.txt", "/etc/passwd", "C:/Windows/x", ".git/config", "sub/../../x", ""]:
            with self.assertRaises(WorkspaceError, msg=bad):
                self.ws.safe(bad)

    def test_apply_and_discard(self):
        self.ws.apply([{"path": "a.txt", "action": "replace", "find": "world", "replace": "AI"},
                       {"path": "new/b.py", "action": "write", "content": "x=1\n"}])
        self.assertIn("AI", (self.root / "a.txt").read_text())
        self.assertTrue((self.root / "new/b.py").exists())
        self.assertIn("+AI", self.ws.diff())
        self.ws.discard()
        self.assertEqual((self.root / "a.txt").read_text(), "hello\nworld\n")
        self.assertFalse((self.root / "new/b.py").exists())

    def test_failed_batch_rolls_back(self):
        self.ws.apply([{"path": "a.txt", "action": "write", "content": "v1\n"}])
        with self.assertRaises(WorkspaceError):
            self.ws.apply([{"path": "a.txt", "action": "write", "content": "v2\n"},
                           {"path": "a.txt", "action": "replace", "find": "없는문장", "replace": "x"}])
        # 실패한 묶음만 되돌리고 앞 묶음(v1)은 유지
        self.assertEqual((self.root / "a.txt").read_text(), "v1\n")
        self.ws.discard()
        self.assertEqual((self.root / "a.txt").read_text(), "hello\nworld\n")

    def test_replace_must_be_unique(self):
        (self.root / "dup.txt").write_text("x x", encoding="utf-8")
        with self.assertRaises(WorkspaceError):
            self.ws.apply([{"path": "dup.txt", "action": "replace", "find": "x", "replace": "y"}])


class FlowTest(unittest.TestCase):
    def test_full_run_with_fix_round_and_commit(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
            git(root, "init", "-q"); git(root, "config", "user.email", "t@t"); git(root, "config", "user.name", "t")
            git(root, "add", "."); git(root, "commit", "-qm", "init")
            reviews = iter([{"verdict": "REQUEST_CHANGES", "issues": ["더하기가 아니라 빼기"], "chat": "버그"},
                            {"verdict": "APPROVE", "issues": [], "chat": "좋습니다"}])
            builds = iter([
                {"edits": [{"path": "README.md", "action": "write", "content": "# calc\n"}], "chat": "README 추가"},
                {"edits": [{"path": "calc.py", "action": "replace", "find": "a - b", "replace": "a + b"}], "chat": "버그 수정"},
            ])

            def mock(system, user):
                if "팀장" in system:
                    return json.dumps({"summary": "s", "chat": "계획", "tasks": [{"title": "README", "instructions": "i", "files": ["README.md", "calc.py"]}]})
                if "개발 담당" in system:
                    return "```json\n" + json.dumps(next(builds)) + "\n```"
                return json.dumps(next(reviews))

            cfg = {"workspace": d, "test_command": f'"{sys.executable}" -c "import calc; assert calc.add(2,3)==5"',
                   "max_tasks": 3, "max_fix_rounds": 2, "max_calls": 12, "lang": "한국어",
                   "roles": {r: {"provider": "mock", "model": "mock"} for r in ("planner", "builder", "reviewer")}}
            events = []
            run = Run(cfg, {}, "README 추가", events.append, mock=mock)
            run.run()
            self.assertEqual(run.state, "waiting_approval", [e["text"] for e in events])
            kinds = [e["kind"] for e in events]
            self.assertEqual(kinds.count("test"), 2)          # 첫 테스트 실패 → 수정 → 통과
            self.assertFalse([e for e in events if e["kind"] == "test"][0]["ok"])
            self.assertTrue([e for e in events if e["kind"] == "test"][1]["ok"])
            msg = run.approve()
            self.assertIn("커밋", msg)
            self.assertIn("ai-office/", git(root, "branch", "--show-current").stdout)
            self.assertIn("a + b", (root / "calc.py").read_text())

    def test_call_limit_stops(self):
        with tempfile.TemporaryDirectory() as d:
            def mock(system, user):
                if "팀장" in system:
                    return json.dumps({"tasks": [{"title": f"t{i}", "instructions": "", "files": []} for i in range(3)]})
                if "개발 담당" in system:
                    return json.dumps({"edits": []})
                return json.dumps({"verdict": "REQUEST_CHANGES", "issues": ["계속"]})
            cfg = {"workspace": d, "test_command": "", "max_tasks": 3, "max_fix_rounds": 5, "max_calls": 5,
                   "roles": {r: {"provider": "mock"} for r in ("planner", "builder", "reviewer")}}
            ev = []
            run = Run(cfg, {}, "x", ev.append, mock=mock)
            run.run()
            self.assertEqual(run.state, "failed")
            self.assertIn("호출 한도", ev[-1]["text"])

    def test_missing_key_message(self):
        with self.assertRaises(providers.ProviderError) as c:
            providers.chat("openai", "", "", "s", "u")
        self.assertIn("키가 비어", str(c.exception))

    def test_parse_json_variants(self):
        self.assertEqual(providers.parse_json('설명\n```json\n{"a":1}\n```'), {"a": 1})
        self.assertEqual(providers.parse_json('앞말 {"a": 2} 뒷말'), {"a": 2})


class ServerTest(unittest.TestCase):
    def test_api_demo_flow(self):
        with tempfile.TemporaryDirectory() as appdata, tempfile.TemporaryDirectory() as work:
            os.environ["APPDATA"] = appdata
            from aio_desktop import server
            httpd = server.serve(0)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{httpd.server_address[1]}"

            def call(path, body=None, token=server.TOKEN):
                req = urllib.request.Request(base + path, method="POST" if body is not None else "GET",
                                             data=json.dumps(body).encode() if body is not None else None,
                                             headers={"x-token": token, "content-type": "application/json"})
                try:
                    with urllib.request.urlopen(req) as r:
                        return r.status, json.loads(r.read())
                except urllib.error.HTTPError as e:
                    return e.code, json.loads(e.read() or b"{}")

            self.assertEqual(call("/api/config", token="wrong")[0], 403)
            page = urllib.request.urlopen(base + "/").read().decode()
            self.assertIn(server.TOKEN, page)
            roles = {r: {"provider": "mock", "model": "mock"} for r in ("planner", "builder", "reviewer")}
            self.assertEqual(call("/api/config", {"workspace": work, "roles": roles, "keys": {"openai": "sk-test"}})[0], 200)
            st, cfg = call("/api/config")
            self.assertTrue(cfg["keys_set"]["openai"])
            self.assertNotIn("sk-test", json.dumps(cfg))           # 키는 화면으로 다시 나가지 않음
            self.assertEqual(call("/api/run", {"task": "데모"})[0], 200)
            for _ in range(50):
                st, ev = call("/api/events?since=0")
                if ev["state"] in ("waiting_approval", "failed"):
                    break
                time.sleep(0.1)
            self.assertEqual(ev["state"], "waiting_approval", ev)
            self.assertTrue((Path(work) / "AI_OFFICE_DEMO.md").exists())
            self.assertIn("AI_OFFICE_DEMO.md", call("/api/diff")[1]["diff"])
            self.assertEqual(call("/api/discard", {})[0], 200)
            self.assertFalse((Path(work) / "AI_OFFICE_DEMO.md").exists())
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main()
