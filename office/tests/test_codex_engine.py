"""codex 엔진 시험. 진짜 codex 대신 가짜 'codex' 스크립트를 PATH 앞에 둔다.
실행: python -m unittest discover -s office/tests -v"""
import importlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

OFFICE = Path(__file__).resolve().parents[1]

FAKE = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
log = os.environ["FAKE_CODEX_LOG"]
with open(log, "w", encoding="utf-8") as f:
    json.dump({"args": args, "prompt": prompt, "has_key": os.environ.get("CODEX_API_KEY") == "sk-test",
               "has_wrapper_key": "AI_OFFICE_CODEX_KEY" in os.environ}, f)
if os.environ.get("FAKE_CODEX_FAIL"):
    print(json.dumps({"type": "turn.failed", "error": {"message": "usage limit reached"}}))
    sys.exit(1)
print(json.dumps({"type": "thread.started", "thread_id": "t"}))
print(json.dumps({"type": "item.completed", "item": {"id": "1", "type": "reasoning", "text": "생각"}}))
print(json.dumps({"type": "item.completed", "item": {"id": "2", "type": "agent_message", "text": "최종 답"}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 120, "cached_input_tokens": 20, "output_tokens": 7}}))
'''


class CodexEngineTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        bin_dir = Path(self.d.name) / "bin"
        bin_dir.mkdir()
        exe = bin_dir / "codex"
        exe.write_text(FAKE, encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        self.log = Path(self.d.name) / "call.json"
        self.env_backup = dict(os.environ)
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        os.environ["FAKE_CODEX_LOG"] = str(self.log)
        os.environ["AI_OFFICE_REPO_ROOT"] = self.d.name
        os.environ["AI_OFFICE_LOG_DIR"] = str(Path(self.d.name) / "logs")
        os.environ["AI_OFFICE_CODEX_KEY"] = "sk-test"
        sys.path.insert(0, str(OFFICE))
        sys.modules.pop("orchestrator", None)
        self.orc = importlib.import_module("orchestrator")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env_backup)
        sys.path.remove(str(OFFICE))
        self.d.cleanup()

    def test_key_removed_from_env_at_start(self):
        self.assertNotIn("AI_OFFICE_CODEX_KEY", os.environ)
        self.assertNotIn("CODEX_API_KEY", os.environ)

    def test_reviewer_is_read_only_and_gets_key_only_in_call(self):
        out = self.orc.run_codex("리뷰해 줘", {"_role": "reviewer", "model": "gpt-x"})
        self.assertEqual(out, "최종 답")
        call = json.loads(self.log.read_text(encoding="utf-8"))
        self.assertIn("read-only", call["args"])
        self.assertEqual(call["args"][-1], "-")
        self.assertIn("gpt-x", call["args"])
        self.assertEqual(call["prompt"], "리뷰해 줘")
        self.assertTrue(call["has_key"])
        self.assertFalse(call["has_wrapper_key"])
        self.assertNotIn("CODEX_API_KEY", os.environ)   # 호출 뒤에도 부모 환경엔 없음
        self.assertEqual(self.orc._last_meta["usage"],
                         {"input_tokens": 120, "output_tokens": 7, "cache_read_input_tokens": 20})

    def test_builder_can_write(self):
        self.orc.run_codex("고쳐 줘", {"_role": "builder"})
        call = json.loads(self.log.read_text(encoding="utf-8"))
        self.assertIn("workspace-write", call["args"])

    def test_failure_raises_for_fallback(self):
        os.environ["FAKE_CODEX_FAIL"] = "1"
        with self.assertRaises(RuntimeError) as cm:
            self.orc.run_codex("x", {"_role": "reviewer"})
        self.assertIn("usage limit", str(cm.exception))

    def test_missing_key(self):
        self.orc._CODEX_KEY = ""
        with self.assertRaises(RuntimeError) as cm:
            self.orc.run_codex("x", {"_role": "reviewer"})
        self.assertIn("CODEX_API_KEY", str(cm.exception))

    def test_tests_subprocess_cannot_see_key(self):
        p = self.orc.sh([sys.executable, "-c", "import os;print(os.environ.get('CODEX_API_KEY','')+os.environ.get('AI_OFFICE_CODEX_KEY',''))"])
        self.assertEqual(p.stdout.strip(), "")

    def test_call_uses_fallback_and_sets_role(self):
        os.environ["FAKE_CODEX_FAIL"] = "1"
        cfg = {"roles": {"reviewer": {"engine": "codex", "fallback_engine": "gemini"}}}
        seen = {}
        def fake_gemini(prompt, role):
            seen.update(role)
            return "대체 답"
        self.orc.ENGINES["gemini"] = fake_gemini
        self.assertEqual(self.orc.call("reviewer", "p", cfg), "대체 답")
        self.assertEqual(seen.get("_role"), "reviewer")


if __name__ == "__main__":
    unittest.main()
