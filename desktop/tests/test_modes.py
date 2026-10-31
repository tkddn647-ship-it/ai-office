"""보안·자소서 모드 시험. 네트워크 없이 가짜 공개 API(fetch)와 가짜 AI로 돈다."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
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


def make_repo(root: Path):
    """for_office와 같은 모양의 작은 저장소."""
    (root / "experiences").mkdir(parents=True)
    (root / "experiences" / "EXP-01-rocket.md").write_text("# EXP-01: 로켓\n추력 측정 [확인 필요: 기간]\n", encoding="utf-8")
    (root / "experiences" / "EXP-02-car.md").write_text("# EXP-02: 레이싱카\n강화학습\n", encoding="utf-8")
    (root / "experiences" / "README.md").write_text("무시", encoding="utf-8")
    a = root / "applications" / "A사-개발"
    a.mkdir(parents=True)
    (a / "questions.yaml").write_text("company: A사\nrole: 개발\nresult: \"제출 완료\"\ndefense: true\nquestions:\n"
                                      "  - {id: 1, title: 지원동기, text: \"지원 동기?\", limit: 800, count: with_spaces}\n"
                                      "  - id: 2\n    title: 역량\n    text: \"역량?\"\n    limit: 600\n    count: without_spaces\n", encoding="utf-8")
    (a / "Q1.md").write_text("[초안 1]\n초안 [EXP-01]", encoding="utf-8")
    (root / "applications" / "_template").mkdir()
    (root / "applications" / "_template" / "questions.yaml").write_text("company: 회사명\nquestions: [{id: 1, text: x}]", encoding="utf-8")
    (root / "final" / "A사-개발").mkdir(parents=True)
    (root / "final" / "A사-개발" / "Q1.md").write_text("[최종 1]\n최종본 [EXP-01] [JD]", encoding="utf-8")
    (root / "jd" / "A사").mkdir(parents=True)
    (root / "jd" / "A사" / "공고.md").write_text("담당업무: 개발", encoding="utf-8")
    (root / "guides" / "sources").mkdir(parents=True)
    (root / "guides" / "G1-방산.md").write_text(G1, encoding="utf-8")
    (root / "guides" / "G2-현대차.md").write_text(G2, encoding="utf-8")
    (root / "guides" / "sources" / "examples.txt").write_text(EXAMPLES, encoding="utf-8")
    (root / "guides" / "sources" / "G1-원본.pdf").write_bytes(b"%PDF")


G1 = """# G1
## 1. 경험 고르기·쓰기
- 트러블슈팅 중심 [G1 p.80]
## 2. 방산 어필 = 같은 경험을 '방산 관점'으로 [G1 p.81-83]
- 멈추지 않는 시스템
## 5. 글쓰기 원칙
- 두괄식 [G1 p.86]
## 6. 항목별 구조 (강의 예시에서 뽑은 뼈대)
- 도전: 첫 문장에 결과
### ⚠ 예시를 그대로 따라 하면 안 되는 부분 (강의 규칙과 충돌)
- 다짐형 맺음
## 8. 퇴고 체크 [G1 p.96]
- 첫 문장만 봐도
"""
G2 = """# G2
## 1. 대원칙: 면접에서 공격받지 않는 자소서 [G2 p.41]
- 답할 수 있는 것만
## 2. 문항 유형별 작성법
### 지원동기 (+ 입사 후 꿈·성장) [G2 p.28-32]
- JD 재해석이 절반 [p.29]
### 직무역량 [G2 p.43]
- 전문 용어로
### 강점·약점 [G2 p.44]
- 꼼꼼함 금지
## 4. JD 읽는 법 [G2 p.34-40]
- 현업 용어
"""
EXAMPLES = "# 예시\n## G2 p.45\n저는 이 목표를 달성하기 위해 기술적 성장과 비즈니스 수익성을 통합 관리하는 전략적 PM으로 기여하고자 지원했습니다.\n"


class GuideTest(unittest.TestCase):
    def test_plan_and_copy(self):
        from aio_desktop.guides import Guides, qtype
        with tempfile.TemporaryDirectory() as d:
            make_repo(Path(d))
            g = Guides(d)
            qs = [{"id": 1, "title": "지원동기", "text": "입사 후 포부"}, {"id": 2, "title": "", "text": "본인의 장점과 약점"},
                  {"id": 3, "title": "도전", "text": "도전 경험을 쓰시오"}]
            p = g.plan(qs, defense=True)
            self.assertEqual([p["per_q"][i]["type"] for i in (1, 2, 3)], ["motivation", "strengths", "experience"])
            self.assertIn("G2 지원동기 (+ 입사 후 꿈·성장)", p["per_q"][1]["sections"])
            self.assertIn("G2 4. JD 읽는 법", p["per_q"][1]["sections"])
            self.assertIn("G1 ⚠ 예시를 그대로 따라 하면 안 되는 부분 (강의 규칙과 충돌)", p["per_q"][3]["sections"])
            self.assertIn("G1 2. 방산 어필 = 같은 경험을 '방산 관점'으로", p["per_q"][2]["sections"])   # 방산이면 모든 문항
            self.assertNotIn("방산 어필", g.plan(qs, defense=False)["text"])
            self.assertEqual(qtype({"title": "단점 / 약점 / 극복 경험", "text": ""}), "strengths")
            hit = g.copied("[제목 1]\n그래서 저는 이 목표를 달성하기 위해 기술적 성장과 비즈니스 수익성을 함께 봅니다.")
            self.assertEqual(hit[0][1], "G2 p.45")
            self.assertEqual(g.copied("[제목 1]\n저는 로드셀로 추력을 직접 쟀습니다."), [])

    def test_guides_reach_writer_and_rule_check(self):
        from aio_desktop import importer
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as data:
            make_repo(Path(src))
            lib = Library(data)
            rep = importer.apply(lib, importer.read_source("folder", src))
            self.assertEqual(sorted(rep["guide_new"]), ["G1-방산.md", "G2-현대차.md", "examples.txt"])
            self.assertFalse((Path(data) / "guides" / "sources" / "G1-원본.pdf").exists())     # PDF는 안 가져옴
            seen = []

            def mock(system, user):
                seen.append((system, user))
                if "자기소개서를 함께 쓰는" in system:
                    return json.dumps({"answers": [
                        {"id": 1, "text": "[매출 3배]\n저는 이 목표를 달성하기 위해 기술적 성장과 비즈니스 수익성을 통합 관리합니다. [EXP-01]"},
                        {"id": 2, "text": "[측정 3회]\n로드셀로 쟀습니다. [EXP-02]"}]})
                return json.dumps({"questions": [{"id": 1, "score": 4, "ai": 1}, {"id": 2, "score": 4, "ai": 1}]})
            run = jaso.JasoRun({"roles": ROLES}, {}, lib, "A사-개발", lambda e: None, mock=mock, mode="write")
            run.app["max_rounds"] = 1
            run.run()
            w = seen[0][1]
            self.assertIn("Q1: 유형 '지원동기·입사 후 포부' → 따를 절: G2 지원동기", w)
            self.assertIn("JD 재해석이 절반", w)
            self.assertNotIn("기술적 성장과 비즈니스", w)                 # 예시 원문은 AI에 보내지 않음
            self.assertIn("가이드 위반을 지적할 때는", seen[1][0])          # 심사관도 가이드 기준
            errs = run.rounds[0]["mech"]["errors"]
            self.assertTrue(any("강의 예시와 20자 이상 같음 (G2 p.45)" in e for e in errs), errs)
            self.assertFalse(run.rounds[0]["ok"])


class ReaderTest(unittest.TestCase):
    def test_prepare_kinds(self):
        import base64, io, zipfile
        from aio_desktop import reader
        dx = io.BytesIO()
        with zipfile.ZipFile(dx, "w") as z:
            z.writestr("word/document.xml", "<w:document><w:p><w:r><w:t>담당업무 &amp; 자격</w:t></w:r></w:p><w:p><w:t>둘째 줄</w:t></w:p></w:document>")
        enc = lambda b: base64.b64encode(b).decode()
        send, text, raw = reader.prepare([
            {"name": "공고.png", "mime": "image/png", "data": enc(b"\x89PNG fake")},
            {"name": "../../문항.pdf", "mime": "application/pdf", "data": enc(b"%PDF-1.4")},
            {"name": "메모.txt", "mime": "text/plain", "data": enc("글 메모".encode())},
            {"name": "자소서.docx", "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "data": enc(dx.getvalue())}])
        self.assertEqual([f["mime"] for f in send], ["image/png", "application/pdf"])
        self.assertEqual(raw[1]["name"], "문항.pdf")                        # 경로는 버리고 이름만
        self.assertIn("글 메모", text)
        self.assertIn("담당업무 & 자격\n둘째 줄", text)
        with self.assertRaises(reader.ReadError) as c:
            reader.prepare([{"name": "a.hwp", "mime": "", "data": enc(b"x")}])
        self.assertIn("PDF로 저장", str(c.exception))
        with self.assertRaises(reader.ReadError):
            reader.prepare([{"name": "a.exe", "mime": "application/x-msdownload", "data": enc(b"MZ")}])

    def test_read_sends_files_and_keeps_originals(self):
        import base64
        from aio_desktop import providers, reader
        with tempfile.TemporaryDirectory() as d:
            lib = Library(d)
            got = {}

            def fake_chat(provider, model, key, system, user, mock=None, files=None, timeout=0, **k):
                got.update(system=system, user=user, files=files)
                return json.dumps({"company": "B사", "jd_text": "원문", "questions": [{"text": "지원동기", "limit": 700, "count": "without_spaces"}]}), {}
            orig = providers.chat
            providers.chat = fake_chat
            try:
                r = reader.read({"roles": ROLES}, {}, lib, "jd",
                                [{"name": "q.jpg", "mime": "image/jpeg", "data": base64.b64encode(b"jpg").decode()}], "추가 글")
            finally:
                providers.chat = orig
            self.assertIn("글자 그대로", got["system"])
            self.assertEqual(got["files"][0]["name"], "q.jpg")
            self.assertIn("추가 글", got["user"])
            self.assertEqual(r["company"], "B사")
            self.assertTrue(list(Path(r["_saved_originals"]).glob("q.jpg")))

    def test_save_own_guide_and_plan(self):
        from aio_desktop import reader
        from aio_desktop.guides import Guides
        with tempfile.TemporaryDirectory() as d:
            make_repo(Path(d))                       # G1·G2도 있는 상태에서
            lib = Library(d)
            name = reader.save_guide(lib, "학교 특강", "## 1. 숫자로 쓰기 [p.2]\n- 결과 숫자\n\n## 문항별: 지원동기 [p.5]\n- 회사 제품 이야기\n\n## 문항별: 강점·약점\n- 사례로",
                                     "## p.5\n저는 귀사의 비전에 깊이 공감하여 지원하게 되었으며 반드시 기여하겠습니다")
            self.assertEqual(name, "학교-특강.md")
            g = Guides(d)
            p = g.plan([{"id": 1, "title": "지원동기", "text": ""}, {"id": 2, "title": "도전", "text": "도전 경험"}], False)
            s1, s2 = p["per_q"][1]["sections"], p["per_q"][2]["sections"]
            self.assertIn("학교-특강 문항별: 지원동기", s1)
            self.assertNotIn("학교-특강 문항별: 지원동기", s2)
            self.assertNotIn("학교-특강 문항별: 강점·약점", s1 + s2)
            self.assertIn("학교-특강 1. 숫자로 쓰기", s2)               # 일반 절은 모든 문항
            self.assertIn("G2 지원동기 (+ 입사 후 꿈·성장)", s1)         # 기존 G1·G2도 그대로
            self.assertEqual(g.copied("[x 1]\n저는 귀사의 비전에 깊이 공감하여 지원하게 되었습니다")[0][1], "학교-특강 p.5")


class ImportTest(unittest.TestCase):
    def test_folder_zip_tar_same_result(self):
        import io, tarfile, zipfile
        from aio_desktop import importer
        with tempfile.TemporaryDirectory() as src:
            make_repo(Path(src) / "for_office-main")
            repo = Path(src) / "for_office-main"
            zb = io.BytesIO()
            with zipfile.ZipFile(zb, "w") as z:
                for p in repo.rglob("*"):
                    if p.is_file():
                        z.write(p, "for_office-main/" + p.relative_to(repo).as_posix())
            tb = io.BytesIO()
            with tarfile.open(fileobj=tb, mode="w:gz") as t:
                t.add(repo, arcname="o-for_office-abc123")
                evil = tarfile.TarInfo("o-for_office-abc123/experiences/EXP-09-link.md"); evil.type = tarfile.SYMTYPE; evil.linkname = "/etc/passwd"
                t.addfile(evil)
                big = tarfile.TarInfo("o-for_office-abc123/../../escape.md"); data = b"x"; big.size = 1
                t.addfile(big, io.BytesIO(data))
            a = importer._from_dir(repo)
            b = importer._from_zip(zb.getvalue())
            c = importer._from_tar(tb.getvalue())
            self.assertEqual(set(a), set(b))
            self.assertEqual(set(a), set(c))                         # 링크·바깥 경로는 빠짐
            self.assertNotIn("experiences/README.md", a)
            self.assertFalse(any(k.startswith("applications/_template") for k in a))

    def test_apply_and_reapply(self):
        from aio_desktop import importer
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as data:
            make_repo(Path(src))
            lib = Library(data)
            lib.exp_save(None, "내가 앱에서 쓴 것", "로컬")             # EXP-01 (로컬) → 저장소 것으로 바뀌고 백업
            files = importer.read_source("folder", src)
            rep = importer.apply(lib, files, source="folder")
            self.assertEqual((rep["exp_new"], rep["exp_updated"]), (["EXP-02"], ["EXP-01"]))
            self.assertTrue(list(Path(rep["backup"]).rglob("EXP-01-*.md")))
            self.assertTrue((Path(data) / "experiences" / "EXP-01-rocket.md").exists())
            self.assertEqual(len(list((Path(data) / "experiences").glob("EXP-01*"))), 1)
            self.assertEqual(rep["app_new"], ["A사-개발"])
            app = lib.app_full("A사-개발")
            self.assertEqual([q["limit"] for q in app["questions"]], [800, 600])
            self.assertEqual(app["questions"][1]["count"], "without_spaces")
            self.assertTrue(app["defense"])
            self.assertEqual(app["status"], "제출 완료")
            self.assertEqual(app["jd"], "담당업무: 개발")
            self.assertIn("최종본", app["answers"]["1"])                 # final/ 이 우선
            self.assertEqual((Path(data) / "applications" / "A사-개발" / "Q1.제출용.txt").read_text(encoding="utf-8"), "[최종 1]\n최종본")
            # 내가 앱에서 정한 목표 점수는 다시 가져와도 유지
            d = json.loads((Path(data) / "applications" / "A사-개발" / "app.json").read_text(encoding="utf-8"))
            d["target_ai"] = 1
            (Path(data) / "applications" / "A사-개발" / "app.json").write_text(json.dumps(d), encoding="utf-8")
            rep2 = importer.apply(lib, files)
            self.assertEqual((rep2["exp_same"], rep2["exp_new"], rep2["exp_updated"]), (2, [], []))
            (Path(src) / "final" / "A사-개발" / "Q1.md").write_text("[최종 2]\n바뀜", encoding="utf-8")
            rep3 = importer.apply(lib, importer.read_source("folder", src))
            self.assertEqual(rep3["app_updated"], ["A사-개발"])
            self.assertEqual(lib.app_full("A사-개발")["target_ai"], 1)

    def test_github_errors(self):
        from aio_desktop import importer
        with self.assertRaises(importer.ImportError_):
            importer.fetch_github("../../etc", "main", "")
        def deny(req):
            raise urllib.error.HTTPError(req.full_url, 404, "x", {}, None)
        with self.assertRaises(importer.ImportError_) as c:
            importer.fetch_github("me/private", "main", "", opener=deny)
        self.assertIn("토큰", str(c.exception))


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
            import io, tarfile
            with tempfile.TemporaryDirectory() as src:
                make_repo(Path(src))
                tb = io.BytesIO()
                with tarfile.open(fileobj=tb, mode="w:gz") as t:
                    t.add(src, arcname="me-repo-sha")
            seen_auth = []
            httpd.gh_opener = lambda req: (seen_auth.append(req.get_header("Authorization")), tb.getvalue())[1]
            call("/api/config", {"keys": {"github": "ghp_test"}})
            self.assertNotIn("ghp_test", json.dumps(call("/api/config")[1]))
            st, rep = call("/api/import", {"kind": "github", "value": "me/repo", "branch": "main", "auto": True})
            self.assertEqual(st, 200, rep)
            self.assertEqual(seen_auth, ["Bearer ghp_test"])
            self.assertEqual(rep["exp_new"], ["EXP-01", "EXP-02"])
            self.assertEqual(call("/api/import", {"startup": True})[1]["exp_same"], 2)   # 켤 때 자동: 첫 번째만 실제로 가져옴
            self.assertTrue(call("/api/import", {"startup": True})[1]["not_run"])
            self.assertEqual(call("/api/exp/save", {"title": "로켓", "body": "기록"})[1]["id"], "EXP-03")
            draft = call("/api/exp/organize", {"notes": "학회 로켓팀 추력 측정"})[1]
            self.assertIn("[확인 필요", draft["body"])
            self.assertEqual(len(call("/api/lib")[1]["experiences"]), 3)      # 가져온 2개 + 저장한 1개, 초안은 저장 안 됨
            name = call("/api/app/save", {"company": "회사", "role": "개발", "jd": "공고", "questions": [{"text": "지원동기", "limit": 500}]})[1]["name"]
            self.assertEqual(call("/api/run", {"mode": "jaso", "app": name})[0], 200)
            ev = wait(("waiting_approval", "failed"))
            self.assertEqual(ev["state"], "waiting_approval", ev)
            self.assertEqual(call("/api/approve", {})[0], 200)
            self.assertTrue(call("/api/app?name=" + urllib.parse.quote(name))[1]["answers"]["1"])
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main()
