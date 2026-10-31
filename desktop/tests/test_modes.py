"""보안·자소서 모드 시험. 네트워크 없이 가짜 공개 API(fetch)와 가짜 AI로 돈다."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aio_desktop import jaso, security       # noqa: E402
from aio_desktop.library import Library, LibraryError  # noqa: E402
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

    def test_library_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            lib = Library(d)
            e1 = lib.exp_save(None, "로켓 추력", "| 기간 | [확인 필요: 기간] |\n로드셀 측정")
            e2 = lib.exp_save(None, "레이싱카", "강화학습")
            self.assertEqual((e1, e2), ("EXP-01", "EXP-02"))
            lib.exp_save("EXP-01", "로켓 추력 측정", "고침")
            ex = {x["id"]: x for x in lib.experiences()}
            self.assertEqual(ex["EXP-01"]["title"], "로켓 추력 측정")
            self.assertEqual(ex["EXP-01"]["unconfirmed"], 0)
            self.assertEqual(ex["EXP-02"]["unconfirmed"], 0)
            lib.exp_delete("EXP-02")
            self.assertEqual([x["id"] for x in lib.experiences()], ["EXP-01"])
            self.assertTrue(list((Path(d) / "_지운것").glob("*EXP-02*")))
            name = lib.app_save({"company": "A사", "role": "SW 개발", "jd": "공고 원문", "questions": [{"text": "지원동기", "limit": 500}, {"text": ""}],
                                 "answers": {"1": "[제목 1]\n본문 [EXP-01]"}})
            a = lib.app_full(name)
            self.assertEqual(len(a["questions"]), 1)
            self.assertEqual(a["jd"], "공고 원문")
            self.assertEqual((Path(d) / "applications" / name / "Q1.제출용.txt").read_text(encoding="utf-8"), "[제목 1]\n본문")
            with self.assertRaises(LibraryError):
                lib.app_read("../../x")

    def test_loop_until_pass_then_save(self):
        with tempfile.TemporaryDirectory() as d:
            lib = Library(d)
            lib.exp_save(None, "로켓", "로켓 추력 측정 기록")
            lib.exp_save(None, "다른 것", "쓰지 않을 경험")
            name = lib.app_save({"company": "A", "role": "B", "jd": "공고", "defense": True, "exp_ids": ["EXP-01"],
                                 "questions": [{"text": "지원동기", "limit": 300}], "max_rounds": 3})
            seen, det_scores = [], iter([4, 1])

            def mock(system, user):
                seen.append((system, user))
                if "자기소개서를 함께 쓰는" in system:
                    return json.dumps({"answers": [{"id": 1, "text": "[추력 12% 개선]\n측정했습니다. [EXP-01]"}]})
                if "AI가 쓴 자기소개서" in system:     # 1차 판별관 4점 → 수정 → 2차 1점
                    return json.dumps({"questions": [{"id": 1, "ai": next(det_scores), "sentences": []}]})
                return json.dumps({"questions": [{"id": 1, "score": 4, "verdict": "통과", "ai": 1}], "overall": ""})
            ev = []
            run = jaso.JasoRun({"roles": ROLES}, {}, lib, name, ev.append, mock=mock)
            run.run()
            self.assertEqual(run.state, "waiting_approval", [e["text"] for e in ev])
            self.assertEqual(len(run.rounds), 2)          # 1차 판별관 AI 4점 → 수정 → 2차 통과
            self.assertEqual(run.rounds[0]["summary"][1]["ai"], 4)        # 면접관 1점, 판별관 4점 → 높은 쪽
            self.assertEqual(sum("AI가 쓴 자기소개서" in x[0] for x in seen), 2)
            self.assertIn("ai_detector", seen[7][1])                  # 판별 결과가 수정 요청에 들어감
            self.assertNotIn("쓰지 않을 경험", seen[0][1])  # 고른 경험만 보냄
            self.assertIn("방산 기업이다", seen[0][0])
            final = [e for e in ev if e["kind"] == "final"][0]["answers"]
            self.assertEqual(final["1"], "[추력 12% 개선]\n측정했습니다. [EXP-01]")
            run.approve()
            self.assertEqual((Path(d) / "applications" / name / "Q1.제출용.txt").read_text(encoding="utf-8"), "[추력 12% 개선]\n측정했습니다.")
            self.assertTrue(list((Path(d) / "applications" / name / "심사기록").glob("*.json")))

    def _lib_with_answer(self, d, text="[제목 1]\n내 글 [EXP-01]"):
        lib = Library(d)
        lib.exp_save(None, "로켓", "기록")
        return lib, lib.app_save({"company": "A", "jd": "담당업무: 개발", "questions": [{"text": "q", "limit": 300}], "answers": {"1": text}})

    def test_screen_only_does_not_rewrite(self):
        with tempfile.TemporaryDirectory() as d:
            lib, name = self._lib_with_answer(d)
            calls, ev = [], []

            def mock(system, user):
                calls.append(system)
                extra = {"requirements": [{"item": "개발", "status": "충족"}, {"item": "C++", "status": "없음"}]} if "공고 대조" in system else {}
                return json.dumps({"questions": [{"id": 1, "score": 3, "verdict": "탈락" if "인사담당" in system else "보류", "ai": 4}], **extra})
            run = jaso.JasoRun({"roles": ROLES}, {}, lib, name, ev.append, mock=mock, mode="screen")
            run.run()
            self.assertEqual(run.state, "done")
            self.assertEqual(len(calls), 5)                                 # 심사관 5명만, 판별관·작성 없음
            self.assertFalse(any("자기소개서를 함께 쓰는" in c or "AI가 쓴 자기소개서" in c for c in calls))
            dec = [e for e in ev if e["kind"] == "scores"][0]["decision"]
            self.assertEqual(dec["label"], "탈락 위험")
            self.assertEqual((dec["req_total"], dec["req_met"], dec["req_missing"]), (2, 1, ["C++"]))
            self.assertIn("내 글", lib.app_read(name)["answers"]["1"])

    def test_aicheck_only(self):
        with tempfile.TemporaryDirectory() as d:
            lib, name = self._lib_with_answer(d)
            calls, ev = [], []

            def mock(system, user):
                calls.append(system)
                return json.dumps({"questions": [{"id": 1, "ai": 2, "sentences": [{"quote": "내 글", "why": "w", "how": "h"}]}]})
            run = jaso.JasoRun({"roles": ROLES}, {}, lib, name, ev.append, mock=mock, mode="aicheck")
            run.run()
            self.assertEqual(run.state, "done", [e["text"] for e in ev])
            self.assertEqual(len(calls), 1)
            self.assertIn("AI가 쓴 자기소개서", calls[0])
            a = [e for e in ev if e["kind"] == "aicheck"][0]
            self.assertEqual(a["summary"][1]["ai"], 2)
            self.assertIn(1, a["detect"]["signals"])

    def test_ai_signals(self):
        ai_like = ("[역량 3배 성장]\n또한 다양한 경험을 통해 역량을 키웠습니다. 나아가 이를 바탕으로 가치를 만들었습니다.\n\n"
                   "이를 통해 소통 역량과 협업 역량의 중요성을 깨달았습니다.\n\n저는 성장하는 인재가 되겠습니다.")
        r = jaso.ai_signals(ai_like)
        self.assertTrue(any("연결어" in f for f in r["flags"]), r)
        self.assertTrue(any("추상 명사" in f for f in r["flags"]), r)
        self.assertTrue(any("다짐" in f for f in r["flags"]), r)
        human = "[추력 12% 개선]\n로드셀 값이 이상해서 밤새 배선을 다시 봤습니다. 접지가 빠져 있었고, 고친 뒤 3번 다시 쟀습니다."
        self.assertLessEqual(jaso.ai_signals(human)["rule_score"], 1)


class ServerModesTest(unittest.TestCase):
    def test_security_and_jaso_via_api(self):
        with tempfile.TemporaryDirectory() as appdata, tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory() as exp:
            os.environ["APPDATA"] = appdata
            from aio_desktop import providers, server
            (Path(work) / "requirements.txt").write_text("requests==2.0.0\n", encoding="utf-8")

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

            call("/api/config", {"workspace": work, "roles": ROLES, "security": {"exposed": True, "data_level": 3}, "data_dir": exp})
            self.assertEqual(call("/api/run", {"mode": "security"})[0], 200)
            ev = wait(("done", "failed"))
            self.assertEqual(ev["state"], "done", ev)
            self.assertTrue([e for e in ev["events"] if e["kind"] == "report"])
            self.assertIn("requests", call("/api/fix_task", {})[1]["task"])
            self.assertEqual(call("/api/approve", {})[0], 409)
            self.assertEqual(call("/api/exp/save", {"title": "로켓", "body": "기록"})[1]["id"], "EXP-01")
            draft = call("/api/exp/organize", {"notes": "학회 로켓팀 추력 측정"})[1]
            self.assertIn("[확인 필요", draft["body"])
            self.assertEqual(len(call("/api/lib")[1]["experiences"]), 1)      # 초안은 저장 안 됨
            name = call("/api/app/save", {"company": "회사", "role": "개발", "jd": "공고", "questions": [{"text": "지원동기", "limit": 500}]})[1]["name"]
            self.assertEqual(call("/api/run", {"mode": "jaso", "app": name})[0], 200)
            ev = wait(("waiting_approval", "failed"))
            self.assertEqual(ev["state"], "waiting_approval", ev)
            self.assertEqual(call("/api/approve", {})[0], 200)
            self.assertTrue(call("/api/app?name=" + urllib.parse.quote(name))[1]["answers"]["1"])
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main()
