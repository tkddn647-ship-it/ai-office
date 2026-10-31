"""자소서 모드: 작성 → 기계 점검 → 심사관 5명 → 기준을 넘을 때까지 수정 → 사람 확인 후 저장.

원칙 (for_office 규칙에서 가져옴)
- 경험의 근거는 사용자가 고른 '경험 기록 폴더'의 파일뿐. 거기 없는 수치·역할은 쓰지 않고 [확인 필요]로 남긴다.
- 문단마다 근거 파일 표시 [파일이름]을 붙이고, 제출용 글에서는 지운다.
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
from .workspace import Workspace

TAG = re.compile(r"\s*\[(?!확인 필요)([^\[\]\n]{1,60})\]")
CONFIRM = re.compile(r"\[확인 필요[^\]]*\]")

WRITER = """너는 신입 지원자의 자기소개서를 함께 쓰는 사람이다. 지원자 본인이 면접에서 방어할 수 있는 문장만 쓴다.
규칙:
- 경험·수치·역할의 근거는 아래 '경험 기록' 파일뿐이다. 기록에 없는 디테일은 지어내지 말고 [확인 필요: 무엇]으로 남긴다.
- 문단 끝마다 근거 파일 이름을 [파일이름] 형식으로 붙인다(확장자 제외). 회사·공고 근거는 [공고].
- 문항마다 첫 줄은 대괄호 소제목 [ ... ]이고 소제목에 숫자나 정량 결과가 들어간다. 두괄식.
- 한 자소서 안에서 같은 경험 파일을 두 문항의 중심 소재로 쓰지 않는다.
- 본문에 괄호 설명을 쓰지 않는다. '하였습니다'는 전체 5번 이하, 다양한·열정·노력은 합계 3번 이하.
- 문항마다 글자수 제한의 85~100%를 목표로 하되 채우기용 문장은 쓰지 않는다.
- 시뮬레이션 결과는 시뮬레이션이라고 쓰고, 팀 성과와 본인 역할을 구분한다.
- 지원동기는 공고를 내 말로 다시 해석한 내용이 절반 이상이고, 왜 이 회사인지가 들어간다.
- 소리 내어 읽었을 때 사람이 쓴 말처럼 자연스럽게. 문단마다 같은 틀, 교훈·다짐형 끝맺음, 연결어 남발을 피한다.
답은 JSON 하나만: {{"answers": [{{"id": 문항번호, "text": "소제목 줄 + 본문(근거 표시 포함)"}}], "note": "확인이 필요한 점 한두 줄"}}
모든 글은 {lang}로."""

REVISER = WRITER + """
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
        tags = body_tags(raw) - {"공고"}
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
    def __init__(self, cfg: dict, keys: dict, opts: dict, on_event, mock=None):
        self.cfg, self.keys, self.opts, self.on_event, self.mock = cfg, keys, opts, on_event, mock
        self.exp_ws = Workspace(opts["exp_dir"])
        self.questions = [{"id": i + 1, "text": q["text"], "limit": int(q.get("limit") or 1000),
                           "count": q.get("count") or "with_spaces"} for i, q in enumerate(opts.get("questions") or []) if q.get("text")]
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
        # 이 프로그램이 저장한 결과물(ai-office-*)은 근거로 쓰지 않는다
        files = [f for f in self.exp_ws.files() if f.lower().endswith((".md", ".txt")) and not f.replace("\\", "/").startswith("ai-office-")]
        ids = {Path(f).stem for f in files}
        exp = self.exp_ws.bundle(files, budget=90_000)
        q = "\n".join(f"Q{x['id']}. {x['text']} ({x['limit']}자, {'공백 제외' if x['count'] == 'without_spaces' else '공백 포함'})" for x in self.questions)
        mat = (f"# 지원\n{self.opts.get('company', '')} / {self.opts.get('role', '')}\n\n# 공고\n{self.opts.get('jd', '')}\n\n"
               f"# 회사 정보\n{self.opts.get('company_info', '') or '(없음)'}\n\n# 문항\n{q}\n\n# 경험 기록\n{exp}")
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
                raise RuntimeError("문항을 하나 이상 넣어 주세요")
            mat, ids = self.materials()
            if not ids:
                raise RuntimeError("경험 기록 폴더에 .md 또는 .txt 파일이 없습니다")
            self.emit("system", f"경험 기록 {len(ids)}개를 근거로 씁니다. 내용은 고른 AI 회사로 전송됩니다.", "info")
            lang = self.cfg.get("lang", "한국어")
            target_ai = float(self.opts.get("target_ai", 2))
            target_avg = float(self.opts.get("target_avg", 3.5))
            max_rounds = int(self.opts.get("max_rounds", 3))

            d = self.ask("writer", WRITER.format(lang=lang), mat)
            answers = {int(a["id"]): a.get("text", "") for a in d.get("answers", [])}
            self.emit("writer", "초안을 썼습니다." + (f"\n확인할 점: {d.get('note')}" if d.get("note") else ""), "chat")

            for rnd in range(1, max_rounds + 1):
                mech = mech_check(answers, self.questions, ids)
                pnl = self.panel(answers, mat, mech)
                summ = self.summarize(pnl, self.questions)
                ok = (not mech["errors"]) and all(
                    s["ai"] is not None and s["ai"] <= target_ai and s["avg"] is not None and s["avg"] >= target_avg for s in summ.values())
                rec = {"round": rnd, "answers": dict(answers), "mech": mech, "panel": pnl, "summary": summ, "ok": ok}
                self.rounds.append(rec)
                self.emit("judge", f"{rnd}차 심사", "scores", round=rnd, summary=summ, mech=mech,
                          panel={k: v for k, v in pnl.items()}, questions=self.questions)
                if self.best is None or self._better(rec, self.best):
                    self.best = rec
                if ok:
                    self.emit("system", f"{rnd}차에서 기준(AI 의심 ≤ {target_ai:g}, 평균 ≥ {target_avg:g}, 기계 점검 오류 0)을 넘었습니다.", "info")
                    break
                if rnd == max_rounds:
                    self.emit("system", f"{max_rounds}차까지 기준을 다 넘지 못했습니다. 가장 나은 {self.best['round']}차 결과를 보여 드립니다.", "info")
                    break
                fb = {k: {"overall": v.get("overall"), "questions": v.get("questions")} for k, v in pnl.items() if "error" not in v}
                user = (f"{mat}\n\n# 지금 답\n" + "\n\n".join(f"## Q{k}\n{v}" for k, v in answers.items()) +
                        f"\n\n# 기계 점검\n{json.dumps(mech, ensure_ascii=False)}\n\n# 심사 결과\n{json.dumps(fb, ensure_ascii=False)[:30000]}")
                d = self.ask("writer", REVISER.format(lang=lang), user)
                new = {int(a["id"]): a.get("text", "") for a in d.get("answers", [])}
                answers.update({k: v for k, v in new.items() if v.strip()})
                self.emit("writer", f"{rnd}차 심사를 반영해 고쳤습니다." + (f"\n확인할 점: {d.get('note')}" if d.get("note") else ""), "chat")

            self.state = "waiting_approval"
            final = {q["id"]: strip_tags(self.best["answers"].get(q["id"], "")) for q in self.questions}
            self.emit("system", "최종안입니다. 근거 표시를 뺀 제출용 글이에요. 저장을 누르면 파일로 남깁니다.", "final",
                      final=final, round=self.best["round"], summary=self.best["summary"], questions=self.questions)
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
        out = self.exp_ws.root / "ai-office-jaso" / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        out.mkdir(parents=True, exist_ok=True)
        best = self.best
        for q in self.questions:
            raw = best["answers"].get(q["id"], "")
            (out / f"Q{q['id']}_근거표시.md").write_text(raw, encoding="utf-8")
            (out / f"Q{q['id']}_제출용.txt").write_text(strip_tags(raw), encoding="utf-8")
        (out / "심사기록.json").write_text(json.dumps(self.rounds, ensure_ascii=False, indent=2), encoding="utf-8")
        self.state = "done"
        msg = f"{out}에 저장했습니다."
        self.emit("system", msg, "info")
        return msg

    def discard(self) -> str:
        self.state = "discarded"
        self.emit("system", "저장하지 않고 닫았습니다.", "info")
        return "discarded"
