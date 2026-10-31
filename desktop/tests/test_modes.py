"""보안·자소서 모드 시험. 네트워크 없이 가짜 공개 API(fetch)와 가짜 AI로 돈다."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aio_desktop import jaso, security       # noqa: E402
from aio_desktop.workspace import Workspace  # noqa: E402

ROLES = {r: {"provider": "mock", "model": "mock"} for r in ("planner", "builder", "reviewer", "advisor", "writer", "judge")}


def fake_fetch(url, body=None):
    if url.endswith("/querybatch"):
        res = []
        for q in body["queries"]:
            n = q["package"]["name"]
            res.append({"vulns": [{"id": "GHSA-req"}] if n == "requests" else [{"id": "GHSA-yaml"}] if n == "PyYAML" else []})
        return {"results": res}
    if url.endswith("/vulns/GHSA-req"):
        return {"id": "GHSA-req", "aliases": ["CVE-2099-0001"], "summary": "req bug",
                "database_specific": {"severity": "HIGH"},
                "affected": [{"package": {"ecosystem": "PyPI"}, "ranges": [{"events": [{"introduced": "0"}, {"fixed": "2.32.0"}, {"fixed": "2.10.0"}]}]}]}
    if url.endswith("/vulns/GHSA-yaml"):
        return {"id": "GHSA-yaml", "aliases": ["CVE-2099-0002"], "summary": "yaml bug", "database_specific": {"severity": "LOW"},
                "affected": [{"package": {"ecosystem": "PyPI"}, "ranges": [{"events": [{"fixed": "9.9.9"}]}]}]}
    if "known_exploited" in url:
        return {"vulnerabilities": [{"cveID": "CVE-2099-0001", "dateAdded": "2099-01-01"}]}
    if "epss" in url:
        return {"data": [{"cve": "CVE-2099-0001", "epss": "0.5"}, {"cve": "CVE-2099-0002", "epss": "0.0001"}]}
    if "pypi.org/pypi/PyYAML/9.9.9" in url:
        raise OSError("404")
    if "pypi.org" in url:
        return {}
    raise AssertionError("예상 못 한 주소 " + url)


class SecurityTest(unittest.TestCase):
    def test_grade_rules(self):
        self.assertEqual(security.grade(9.8, True, 0.5, True, 2, True)[0], "긴급")
        self.assertEqual(security.grade(9.8, True, 0.5, False, 2, True)[0], "우선")   # KEV라도 노출 없으면 점수로
        self.assertEqual(security.grade(None, False, None, False, 0, None)[1], 3)
        self.assertEqual(security.grade(2.0, False, 0.0001, False, 0, False)[0], "정기")

    def test_parse_and_vkey(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "requirements.txt").write_text("requests==2.0.0\nflask>=2\n", encoding="utf-8")
            (Path(d) / "package-lock.json").write_text(json.dumps({"packages": {"": {}, "node_modules/@a/b": {"version": "1.0.0"}}}), encoding="utf-8")
            comps = security.parse_components(Workspace(d))
            self.assertEqual({(c["name"], c["ecosystem"]) for c in comps}, {("requests", "PyPI"), ("@a/b", "npm")})
        self.assertLess(security._vkey("2.10.0"), security._vkey("2.32.0"))
        self.assertIsInstance(security._vkey("1.0.0rc1") < security._vkey("1.0.0.post1"), bool)   # 섞여도 안 죽음

    def test_full_scan(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "requirements.txt").write_text("requests==2.0.0\nPyYAML==5.0\nsix==1.0\n", encoding="utf-8")
            (Path(d) / "app.py").write_text("import os\nimport requests\n", encoding="utf-8")
            ev = []
            run = security.SecurityRun({"workspace": d, "roles": ROLES}, {}, {"exposed": True, "data_level": 2}, ev.append,
                                       mock=None, fetch=fake_fetch)
            from aio_desktop import providers
            run.mock = providers.demo_mock
            run.run()
            self.assertEqual(run.state, "done", [e["text"] for e in ev])
            rows = {r["package"]: r for r in run.report["rows"]}
            self.assertEqual(rows["requests"]["grade"], "긴급")
            self.assertEqual(rows["requests"]["fixed"], "2.10.0")
            self.assertEqual(rows["requests"]["trace"], ["app.py:2"])
            self.assertEqual(rows["PyYAML"]["trace"], [])           # yaml import 없음 → 흔적 없음, 1점
            self.assertEqual(rows["PyYAML"]["parts"]["사용 흔적"], 1)
            self.assertTrue((Path(d) / "ai-office-security/report.md").exists())
            bom = json.loads((Path(d) / "ai-office-security/sbom.cdx.json").read_text(encoding="utf-8"))
            self.assertEqual(len(bom["components"]), 3)
            self.assertIn("requests 2.0.0 → 2.10.0", run.fix_task())


class JasoTest(unittest.TestCase):
    def test_strip_keeps_heading(self):
        t = "[불량률 4%→0%: 분석]\n첫 문단입니다. [EXP-01]\n\n둘째 문단. [공고] [확인 필요: 기간]"
        out = jaso.strip_tags(t)
        self.assertTrue(out.startswith("[불량률 4%→0%: 분석]"))
        self.assertNotIn("[EXP-01]", out)
        self.assertIn("[확인 필요: 기간]", out)

    def test_mech_check(self):
        qs = [{"id": 1, "text": "q", "limit": 30, "count": "with_spaces"}, {"id": 2, "text": "q", "limit": 500, "count": "without_spaces"}]
        ans = {1: "[제목 없음]\n아주 긴 문장입니다 아주 긴 문장입니다 아주 긴 문장입니다. [EXP-01]",
               2: "[결과 3배]\n임시 공간(버퍼)을 썼습니다. [EXP-01] [EXP-99]\n\n근거 없는 문단."}
        m = jaso.mech_check(ans, qs, {"EXP-01"})
        txt = " ".join(m["errors"] + m["warnings"])
        self.assertIn("Q1 글자수", txt)
        self.assertIn("Q1 소제목에 숫자", txt)
        self.assertIn("Q2 본문에 괄호", txt)
        self.assertIn("EXP-99", txt)
        self.assertIn("근거 표시 없는 문단 1", txt)
        self.assertIn("문항 [1, 2]", txt)

    def test_loop_until_pass_then_save(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "EXP-01-로켓.md").write_text("로켓 추력 측정 기록", encoding="utf-8")
            (Path(d) / "ai-office-jaso").mkdir()
            (Path(d) / "ai-office-jaso" / "옛결과.md").write_text("이전 결과", encoding="utf-8")
            seen, ai_scores = [], iter([4, 4, 4, 4, 4, 1, 1, 1, 1, 1])

            def mock(system, user):
                seen.append((system, user))
                if "자기소개서를 함께 쓰는" in system:
                    return json.dumps({"answers": [{"id": 1, "text": "[추력 12% 개선]\n측정했습니다. [EXP-01-로켓]"}]})
                a = next(ai_scores)
                return json.dumps({"questions": [{"id": 1, "score": 4, "ai": a}], "overall": ""})
            ev = []
            run = jaso.JasoRun({"roles": ROLES}, {}, {"exp_dir": d, "company": "A", "role": "B", "jd": "공고",
                                                      "questions": [{"text": "지원동기", "limit": 300}], "max_rounds": 3}, ev.append, mock=mock)
            run.run()
            self.assertEqual(run.state, "waiting_approval", [e["text"] for e in ev])
            self.assertEqual(len(run.rounds), 2)          # 1차 AI 4점 → 수정 → 2차 통과
            self.assertNotIn("이전 결과", seen[0][1])     # 저장한 결과물은 근거로 안 씀
            final = [e for e in ev if e["kind"] == "final"][0]["final"]
            self.assertEqual(final[1], "[추력 12% 개선]\n측정했습니다.")
            run.approve()
            outs = list((Path(d) / "ai-office-jaso").glob("*/Q1_제출용.txt"))
            self.assertEqual(outs[0].read_text(encoding="utf-8"), "[추력 12% 개선]\n측정했습니다.")


class ServerModesTest(unittest.TestCase):
    def test_security_and_jaso_via_api(self):
        with tempfile.TemporaryDirectory() as appdata, tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory() as exp:
            os.environ["APPDATA"] = appdata
            from aio_desktop import providers, server
            (Path(work) / "requirements.txt").write_text("requests==2.0.0\n", encoding="utf-8")
            (Path(exp) / "EXP-01.md").write_text("기록", encoding="utf-8")
            httpd = server.serve(0, mock=providers.demo_mock, fetch=fake_fetch)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{httpd.server_address[1]}"

            def call(path, body=None):
                req = urllib.request.Request(base + path, method="POST" if body is not None else "GET",
                                             data=json.dumps(body).encode() if body is not None else None,
                                             headers={"x-token": server.TOKEN, "content-type": "application/json"})
                try:
                    with urllib.request.urlopen(req) as r:
                        return r.status, json.loads(r.read())
                except urllib.error.HTTPError as e:
                    return e.code, json.loads(e.read() or b"{}")

            def wait(states):
                for _ in range(100):
                    st, ev = call("/api/events?since=0")
                    if ev["state"] in states:
                        return ev
                    time.sleep(0.1)
                return ev

            call("/api/config", {"workspace": work, "roles": ROLES, "security": {"exposed": True, "data_level": 3},
                                 "jaso": {"exp_dir": exp, "company": "회사", "questions": [{"text": "지원동기", "limit": 500}]}})
            self.assertEqual(call("/api/run", {"mode": "security"})[0], 200)
            ev = wait(("done", "failed"))
            self.assertEqual(ev["state"], "done", ev)
            self.assertTrue([e for e in ev["events"] if e["kind"] == "report"])
            self.assertIn("requests", call("/api/fix_task", {})[1]["task"])
            self.assertEqual(call("/api/approve", {})[0], 409)
            self.assertEqual(call("/api/run", {"mode": "jaso"})[0], 200)
            ev = wait(("waiting_approval", "failed"))
            self.assertEqual(ev["state"], "waiting_approval", ev)
            self.assertEqual(call("/api/approve", {})[0], 200)
            self.assertTrue(list(Path(exp).glob("ai-office-jaso/*/Q1_제출용.txt")))
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main()
