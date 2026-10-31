"""자소서 모드: 작성 → 기계 점검 → 심사관 5명 → 기준을 넘을 때까지 수정 → 사람 확인 후 저장.

원칙 (for_office 규칙에서 가져옴)
- 경험의 근거는 자료 폴더의 경험 기록(EXP-NN)뿐. 거기 없는 수치·역할은 쓰지 않고 [확인 필요]로 남긴다.
- 문단마다 근거 표시 [EXP-NN]·[공고]·[회사정보]를 붙이고, 제출용 글에서는 지운다.
- 한 자소서 안에서 같은 경험 파일을 두 문항의 중심 소재로 쓰지 않는다.
- 소제목 [ ]에 숫자, 본문 괄호 금지, '하였습니다' 5번 이하, 다양한·열정·노력 합계 3번 이하.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
from pathlib import Path

from . import providers
from .library import EXP_TEMPLATE, Library

TAG = re.compile(r"\s*\[(?!확인 필요)([^\[\]\n]{1,60})\]")
CONFIRM = re.compile(r"\[확인 필요[^\]]*\]")

WRITER = """너는 신입 지원자의 자기소개서를 함께 쓰는 사람이다. 지원자 본인이 면접에서 방어할 수 있는 문장만 쓴다.
규칙:
- 경험·수치·역할의 근거는 아래 '경험 기록'뿐이다. 기록에 없는 디테일은 지어내지 말고 [확인 필요: 무엇]으로 남긴다.
- 경험 기록 안에 [확인 필요]로 표시된 내용은 아직 확인되지 않은 것이니 사실처럼 쓰지 않는다.
- 문단 끝마다 근거를 [EXP-01]처럼 경험 번호로 붙인다. 공고 근거는 [공고], 회사 정보 근거는 [회사정보]. 근거 없는 문단은 쓰지 않는다.
- 회사 정보는 '공고'와 '회사 정보'에 적힌 것만 쓴다. 이름이 비슷한 다른 회사와 섞지 않는다.
- 문항마다 첫 줄은 대괄호 소제목 [ ... ]이고 소제목에 숫자나 정량 결과가 들어간다. 두괄식.
- 한 자소서 안에서 같은 경험 파일을 두 문항의 중심 소재로 쓰지 않는다.
- 본문에 괄호 설명을 쓰지 않는다. '하였습니다'는 전체 5번 이하, 다양한·열정·노력은 합계 3번 이하.
- 문항마다 글자수 제한의 85~100%를 목표로 하되 채우기용 문장은 쓰지 않는다.
- 시뮬레이션 결과는 시뮬레이션이라고 쓰고, 팀 성과와 본인 역할을 구분한다.
- 문항당 경험 1개. 경험은 행동 나열이 아니라 문제 → 원인 분석 → 조치 → 수치 변화. 해결 못 한 문제는 '지금이라면 이렇게 하겠다'로.
- 지원동기는 공고를 내 말로 다시 해석한 내용이 절반 이상이고, 왜 경쟁사가 아니라 이 회사인지가 들어간다.
- 강점·약점은 꼼꼼함·신중함 같은 누구나 가진 말 대신 구체 키워드. 약점에 시간관리·실무경험 부족 금지.
- 입사 후 포부는 단기(직무 적응)와 중장기(전문 영역)를 구체적으로.
- 수필이 아니라 보고서처럼. 미사여구·다짐형 수식어·글자수 채우기 문장 금지. 전문용어는 비전공 인사담당도 흐름을 따라올 수 있게.{defense}
- 소리 내어 읽었을 때 사람이 쓴 말처럼 자연스럽게. 문단마다 같은 틀, 교훈·다짐형 끝맺음, 연결어 남발을 피한다.
답은 JSON 하나만: {{"answers": [{{"id": 문항번호, "text": "소제목 줄 + 본문(근거 표시 포함)"}}], "note": "확인이 필요한 점 한두 줄"}}
모든 글은 {lang}로."""

DEFENSE = """
- 방산 기업이다. 성능·속도보다 신뢰성을 앞세운다: SW는 예외 처리·Fail-safe, HW는 차폐·이중화, 기계는 내환경·생존성.
  요구사항 명세 독해와 검증 절차 설계를 강조한다. VxWorks·DOORS·VectorCAST 같은 도구는 기록에 써 본 적이 있을 때만 경험으로 쓰고, 아니면 학습 계획으로만."""

ORGANIZE = """너는 취업 준비생의 경험 기록을 정리해 주는 사람이다. 아래 메모만 근거로 경험 기록 양식을 채운다.
- 메모에 없는 기간·인원·역할·수치·결과는 절대 지어내지 말고 [확인 필요: 무엇을]로 남긴다.
- 팀이 한 일과 본인이 직접 한 일을 구분한다. 구분이 안 되면 [확인 필요: 내가 직접 한 부분].
- 시뮬레이션 수치와 실측 수치를 구분해 적는다. 메모에서 알 수 없으면 [확인 필요: 시뮬인지 실측인지].
- 면접에서 물어볼 만한 빈 곳을 '면접 대비' 절에 질문으로 3개까지 적는다.
양식:
{template}
답은 JSON 하나만: {{"title": "한 줄 제목", "body": "양식을 채운 마크다운 전체"}}"""

REVISER_TAIL = """
지금은 수정 단계다. 아래 '심사 결과'와 '기계 점검'을 반영해 전체 답을 다시 낸다. 사실을 더하지 말고, 표현·구조·순서만 고친다.
심사관이 기록에 없는 디테일을 요구해도 지어내지 말고 [확인 필요]로 남긴다."""

COMMON = """너는 서류 심사 패널 중 한 명이다. 아래 '너의 역할' 관점으로만 엄격하게 본다. 칭찬보다 탈락 사유를 찾는다.
- 지적은 자소서의 실제 문장을 짧게 인용해서 한다. 자료에 없는 경험·수치를 제안하지 않는다.
- 판단에 자신이 없으면 "확실하지 않음"이라고 쓴다.
답은 JSON 하나만:
{"questions": [{"id": 1, "verdict": "통과|보류|탈락", "score": 1~5, "points": ["\\"인용\\" → 문제"], "fix": "고칠 방향 한 줄"%s}], "overall": "한두 문장"}
모든 문항에 답한다. 한국어로."""

JUDGES = [
    ("hr", "인사담당 (1차 서류)", "하루에 수백 장을 훑는 인사 담당자. 기술 용어를 잘 모른다. 첫 문장만 읽고 무엇을 했고 결과가 얼마인지 보이는지, 문항에 답했는지, 팀 성과와 본인 역할이 구분되는지, 늘어지는 문장이 없는지 본다."),
    ("lead", "현업 팀장", "같이 일할 신입을 고르는 팀장. 기술 용어를 정확히 쓰는지, 경험이 공고 업무와 실제로 이어지는지, 입사 후 바로 맡길 일이 보이는지, 자격요건과 다른 점을 설득하는지 본다."),
    ("interview", "면접관 (검증)", "서류를 들고 면접에서 파고들 사람. 경험 기록과 다르거나 기록에 없는 디테일, 팀 성과를 개인처럼 쓴 곳, 시뮬레이션을 실측처럼 쓴 곳을 찾고 꼬리질문을 Q: 형식으로 남긴다. 또 AI가 써 준 글을 그대로 낸 것 같은지 0~5로 매긴다(0=사람, 3=의심, 5=거의 확실히 AI). 본문 괄호, 문단마다 같은 틀, 교훈·다짐형 끝맺음, 연결어 남발, 셋씩 묶는 나열, 장면 없이 매끄럽기만 한 문장이 AI 티다."),
    ("rival", "경쟁 지원자 비교", "같은 공고에 지원한 신입 100명의 서류를 본 심사위원. 누구나 쓰는 표현·소재인지, 차별점이 첫 문단에 보이는지, 회사 이름만 바꾸면 되는 글인지, 문항끼리 같은 이야기를 반복하는지 본다."),
    ("jd", "공고 대조", "공고의 담당업무·자격요건·우대사항을 하나씩 체크한다. 항목마다 자소서 어디에서 근거를 댔는지, 비어 있는 항목은 무엇인지, 공고에 없는 회사 정보를 사실처럼 썼는지 본다."),
]


def split_head(text: str) -> tuple[str, str]:
    """첫 줄(소제목)과 본문을 나눈다. 소제목의 [ ]는 근거 표시가 아니다."""
    t = text.strip()
    head, _, body = t.partition("\n")
    return head.strip(), body


def body_tags(text: str) -> set[str]:
    return {t.strip() for t in TAG.findall(split_head(text)[1])}


def strip_tags(text: str) -> str:
    """제출용: 본문의 근거 표시만 지운다. 소제목과 [확인 필요]는 그대로 둔다."""
    head, body = split_head(text)
    body = re.sub(r"[ \t]+\n", "\n", TAG.sub("", body)).strip()
    return (head + "\n" + body).strip() if body else head


def count(text: str, mode: str) -> int:
    if mode == "without_spaces":
        return len(re.sub(r"\s", "", text))
    if mode == "bytes":
        return sum(2 if ord(c) > 127 else 1 for c in text)
    return len(text)


def mech_check(answers: dict[int, str], questions: list[dict], exp_ids: set[str]) -> dict:
    """규칙으로만 하는 점검. 반환: {errors: [...], warnings: [...], per_q: {id: {...}}}"""
    errors, warnings, per_q, used = [], [], {}, {}
    full = "\n".join(answers.values())
    sub = strip_tags(full)
    n_hayeot = sub.count("하였습니다")
    n_words = sum(sub.count(w) for w in ("다양한", "열정", "노력"))
    if n_hayeot > 5:
        errors.append(f"'하였습니다' {n_hayeot}번 (5번 이하)")
    if n_words > 3:
        errors.append(f"다양한·열정·노력 합계 {n_words}번 (3번 이하)")
    for q in questions:
        qid = q["id"]
        raw = answers.get(qid, "")
        clean = strip_tags(raw)
        head, body = split_head(clean)
        n = count(clean, q.get("count", "with_spaces"))
        info = {"chars": n, "limit": q["limit"]}
        if n > q["limit"]:
            errors.append(f"Q{qid} 글자수 {n}/{q['limit']} 초과")
        if not (head.startswith("[") and head.endswith("]")):
            errors.append(f"Q{qid} 첫 줄이 [소제목]이 아닙니다")
        elif not re.search(r"\d", head):
            warnings.append(f"Q{qid} 소제목에 숫자가 없습니다")
        if re.search(r"[()（）]", CONFIRM.sub("", body)):
            errors.append(f"Q{qid} 본문에 괄호가 있습니다")
        if CONFIRM.search(raw):
            warnings.append(f"Q{qid}에 [확인 필요]가 남아 있습니다")
        tags = body_tags(raw) - {"공고", "회사정보", "JD"}
        missing = sorted(t for t in tags if t not in exp_ids)
        if missing:
            errors.append(f"Q{qid} 없는 근거 파일: {', '.join(missing)}")
        paras = [p for p in re.split(r"\n\s*\n", split_head(raw)[1]) if p.strip()]
        untagged = sum(1 for p in paras if not TAG.search(p))
        if untagged:
            warnings.append(f"Q{qid} 근거 표시 없는 문단 {untagged}개")
        for t in tags:
            used.setdefault(t, set()).add(qid)
        per_q[qid] = info
    for t, qs in used.items():
        if len(qs) >= 2:
            warnings.append(f"경험 '{t}'가 문항 {sorted(qs)}에 함께 쓰였습니다 (한 문항에만 중심 소재로)")
    return {"errors": errors, "warnings": warnings, "per_q": per_q}


class JasoRun:
    """mode='write': 작성 → 심사 → 수정 반복.  mode='review': 지금 저장된 답을 심사만 (GitHub의 panel과 같음, 고치지 않음)."""

    def __init__(self, cfg: dict, keys: dict, lib: Library, app_name: str, on_event, mock=None, mode: str = "write"):
        self.cfg, self.keys, self.lib, self.on_event, self.mock, self.mode = cfg, keys, lib, on_event, mock, mode
        self.app_name = app_name
        self.app = lib.app_full(app_name)
        self.questions = self.app.get("questions") or []
        self.state = "running"
        self.best = None
        self.rounds: list[dict] = []
        self.calls = 0
        self.stop_flag = threading.Event()

    def emit(self, role, text, kind="chat", **x):
        self.on_event({"time": dt.datetime.now().strftime("%H:%M:%S"), "role": role, "kind": kind, "text": text, **x})

    def ask(self, role, system, user):
        if self.stop_flag.is_set():
            raise RuntimeError("사용자가 멈췄습니다")
        rc = self.cfg["roles"][role]
        self.calls += 1
        text, _ = providers.chat(rc["provider"], rc.get("model", ""), self.keys.get(rc["provider"], ""), system, user, mock=self.mock)
        return providers.parse_json(text)

    def materials(self) -> tuple[str, set[str]]:
        exp, ids = self.lib.exp_bundle(self.app.get("exp_ids") or None)
        a = self.app
        q = "\n".join(f"Q{x['id']}. {x.get('title') + ' — ' if x.get('title') else ''}{x['text']} "
                      f"({x['limit']}{'바이트' if x['count'] == 'bytes' else '자'}, {'공백 제외' if x['count'] == 'without_spaces' else '공백 포함'})"
                      for x in self.questions)
        mat = (f"# 지원\n{a.get('company', '')} / {a.get('role', '')}{' · 방산 기업' if a.get('defense') else ''}\n\n"
               f"# 공고\n{a.get('jd', '') or '(없음)'}\n\n# 회사 정보\n{a.get('company_info', '') or '(없음)'}\n\n"
               f"# 문항\n{q}\n\n# 경험 기록\n{exp[:90000]}")
        return mat, ids

    def panel(self, answers: dict[int, str], mat: str, mech: dict) -> dict:
        shown = "\n\n".join(f"## Q{q['id']}. {q['text']} — {mech['per_q'][q['id']]['chars']}/{q['limit']}자\n{strip_tags(answers.get(q['id'], ''))}" for q in self.questions)
        res = {}
        for key, name, role in JUDGES:
            extra = ', "ai": 0~5, "ai_points": ["\\"인용\\" → 이유"]' if key == "interview" else ""
            system = COMMON % extra + f"\n\n# 너의 역할: {name}\n{role}"
            user = f"{mat}\n\n# 기계 점검\n{json.dumps(mech['errors'] + mech['warnings'], ensure_ascii=False)}\n\n# 평가할 자소서\n{shown}"
            try:
                res[key] = self.ask("judge", system, user)
            except Exception as e:   # noqa: BLE001  심사관 하나 실패는 표시만 하고 계속
                res[key] = {"error": str(e)}
        return res

    @staticmethod
    def summarize(panel: dict, questions: list[dict]) -> dict:
        per = {}
        for q in questions:
            scores, ai = [], None
            for key, *_ in JUDGES:
                for x in (panel.get(key) or {}).get("questions", []):
                    if int(x.get("id", 0)) == q["id"]:
                        try:
                            scores.append(float(x.get("score")))
                        except (TypeError, ValueError):
                            pass
                        if key == "interview" and x.get("ai") is not None:
                            try:
                                ai = float(x["ai"])
                            except (TypeError, ValueError):
                                pass
            per[q["id"]] = {"avg": round(sum(scores) / len(scores), 2) if scores else None, "min": min(scores) if scores else None, "ai": ai}
        return per

    def run(self):
        try:
            if not self.questions:
                raise RuntimeError("문항을 하나 이상 넣고 저장해 주세요")
            mat, ids = self.materials()
            if not ids:
                raise RuntimeError("경험·활동을 하나 이상 저장해 주세요")
            a = self.app
            target_ai = float(a.get("target_ai", 2))
            target_avg = float(a.get("target_avg", 3.5))
            lang = self.cfg.get("lang", "한국어")
            prompt = WRITER.format(lang=lang, defense=DEFENSE if a.get("defense") else "")
            unconf = [e["id"] for e in self.lib.experiences() if e["id"] in ids and e["unconfirmed"]]
            self.emit("system", f"경험 {len(ids)}개({', '.join(sorted(ids))})를 근거로 씁니다. 공고·경험 내용은 고른 AI 회사로 전송됩니다."
                      + (f"\n{', '.join(unconf)}에는 아직 [확인 필요]가 남아 있어, 그 부분은 사실로 쓰지 않습니다." if unconf else ""), "info")

            if self.mode == "review":
                answers = {q["id"]: a["answers"].get(str(q["id"]), "") for q in self.questions}
                if not any(v.strip() for v in answers.values()):
                    raise RuntimeError("심사할 답이 없습니다. 먼저 작성하거나 답 칸에 붙여 넣고 저장해 주세요")
                max_rounds = 1
            else:
                max_rounds = max(1, min(5, int(a.get("max_rounds", 3))))
                d = self.ask("writer", prompt, mat)
                answers = {int(x["id"]): x.get("text", "") for x in d.get("answers", [])}
                self.emit("writer", "초안을 썼습니다." + (f"\n확인할 점: {d.get('note')}" if d.get("note") else ""), "chat")

            for rnd in range(1, max_rounds + 1):
                mech = mech_check(answers, self.questions, ids)
                pnl = self.panel(answers, mat, mech)
                summ = self.summarize(pnl, self.questions)
                ok = (not mech["errors"]) and all(
                    s["ai"] is not None and s["ai"] <= target_ai and s["avg"] is not None and s["avg"] >= target_avg for s in summ.values())
                rec = {"round": rnd, "mode": self.mode, "answers": dict(answers), "mech": mech, "panel": pnl, "summary": summ, "ok": ok}
                self.rounds.append(rec)
                self.emit("judge", f"{rnd}차 심사" if self.mode == "write" else "심사 결과 (고치지 않음)", "scores", round=rnd,
                          summary=summ, mech=mech, panel=pnl, questions=self.questions)
                if self.best is None or self._better(rec, self.best):
                    self.best = rec
                if ok:
                    self.emit("system", f"기준(AI 의심 ≤ {target_ai:g}, 평균 ≥ {target_avg:g}, 규칙 오류 0)을 넘었습니다.", "info")
                    break
                if rnd == max_rounds:
                    if self.mode == "write":
                        self.emit("system", f"{max_rounds}차까지 기준을 다 넘지 못했습니다. 가장 나은 {self.best['round']}차 결과를 보여 드립니다.", "info")
                    break
                fb = {k: {"overall": v.get("overall"), "questions": v.get("questions")} for k, v in pnl.items() if "error" not in v}
                user = (f"{mat}\n\n# 지금 답\n" + "\n\n".join(f"## Q{k}\n{v}" for k, v in answers.items()) +
                        f"\n\n# 기계 점검\n{json.dumps(mech, ensure_ascii=False)}\n\n# 심사 결과\n{json.dumps(fb, ensure_ascii=False)[:30000]}")
                d = self.ask("writer", prompt + REVISER_TAIL, user)
                new = {int(x["id"]): x.get("text", "") for x in d.get("answers", [])}
                answers.update({k: v for k, v in new.items() if v.strip()})
                self.emit("writer", f"{rnd}차 심사를 반영해 고쳤습니다." + (f"\n확인할 점: {d.get('note')}" if d.get("note") else ""), "chat")

            self.lib.review_save(self.app_name, {"time": dt.datetime.now().isoformat(timespec="seconds"), "best_round": self.best["round"],
                                                 "rounds": self.rounds})
            if self.mode == "review":
                self.state = "done"
                return
            self.state = "waiting_approval"
            self.emit("system", "최종안입니다. 왼쪽 답 칸에 채웠으니 읽고 고친 뒤 저장하세요. 저장 전에는 기존 답이 그대로 있습니다.", "final",
                      answers={str(k): v for k, v in self.best["answers"].items()}, round=self.best["round"],
                      summary=self.best["summary"], questions=self.questions)
        except Exception as e:   # noqa: BLE001
            self.state = "failed"
            self.emit("system", f"멈췄습니다: {type(e).__name__}: {e}", "error")

    @staticmethod
    def _better(a: dict, b: dict) -> bool:
        def key(r):
            s = list(r["summary"].values())
            ai = max((x["ai"] if x["ai"] is not None else 5) for x in s)
            avg = sum((x["avg"] or 0) for x in s) / max(1, len(s))
            return (len(r["mech"]["errors"]) == 0, -ai, avg)
        return key(a) > key(b)

    def approve(self) -> str:
        for q in self.questions:
            self.lib.answer_save(self.app_name, q["id"], self.best["answers"].get(q["id"], ""))
        self.state = "done"
        msg = f"{self.app_name} 답을 저장했습니다."
        self.emit("system", msg, "info")
        return msg

    def discard(self) -> str:
        self.state = "discarded"
        self.emit("system", "저장하지 않고 닫았습니다. 기존 답은 그대로입니다.", "info")
        return "discarded"


def organize_experience(cfg: dict, keys: dict, notes: str, mock=None) -> dict:
    """메모 → 경험 기록 양식 초안. 빈 곳은 [확인 필요]. 저장은 사람이 확인한 뒤."""
    rc = cfg["roles"]["writer"]
    text, _ = providers.chat(rc["provider"], rc.get("model", ""), keys.get(rc["provider"], ""),
                             ORGANIZE.format(template=EXP_TEMPLATE), notes, mock=mock)
    d = providers.parse_json(text)
    return {"title": d.get("title", ""), "body": d.get("body", "")}
