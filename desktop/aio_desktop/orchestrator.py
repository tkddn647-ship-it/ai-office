"""기획 → 구현 → 테스트 → 리뷰 → (수정 라운드) → 사람 승인.

GitHub 버전 AI Office와 같은 원칙:
- 기획·리뷰 역할은 파일을 바꾸지 못한다(읽은 내용만 받는다).
- 구현 역할의 편집은 작업 폴더 안에서만 적용되고, 전부 되돌릴 수 있다.
- 병합(커밋)은 사람이 '승인'을 눌러야 한다.
"""
from __future__ import annotations

import datetime as dt
import json
import threading

from . import providers
from .workspace import Workspace, WorkspaceError

PLANNER = """너는 소프트웨어 팀의 팀장(기획 담당)이다. 파일을 고치지 않는다.
사용자 요청과 저장소 파일 목록(필요하면 일부 내용)을 보고 작업을 최대 {max_tasks}개로 나눈다.
각 작업마다 구현 담당이 읽어야 할 파일 경로를 정확히 고른다(목록에 있는 경로만, 새 파일이면 만들 경로).
답은 JSON 하나만:
{{"summary": "한 문장 요약", "chat": "팀원에게 하는 한두 문장 말", "tasks": [{{"title": "...", "instructions": "구체적 지시", "files": ["경로", ...]}}]}}
모든 글은 {lang}로."""

BUILDER = """너는 소프트웨어 팀의 개발 담당이다. 주어진 작업만 구현한다. 요청 밖 리팩터링 금지.
파일 수정은 아래 JSON의 edits로만 한다. 경로는 저장소 기준 상대경로.
- 작은 파일이거나 새 파일: {{"path": "...", "action": "write", "content": "파일 전체 내용"}}
- 큰 파일 일부: {{"path": "...", "action": "replace", "find": "원문에 정확히 1번 나오는 부분", "replace": "바꿀 내용"}}
- 삭제: {{"path": "...", "action": "delete"}}
답은 JSON 하나만:
{{"edits": [...], "note": "무엇을 바꿨는지", "chat": "팀에게 한두 문장"}}
모든 글은 {lang}로."""

REVIEWER = """너는 소프트웨어 팀의 리뷰 담당이다. 파일을 고치지 않는다.
요청, 계획, 변경 diff, 테스트 결과를 보고 판정한다. 테스트가 실패했거나 요청을 못 채웠거나 명백한 버그가 있으면 REQUEST_CHANGES.
답은 JSON 하나만:
{{"verdict": "APPROVE" 또는 "REQUEST_CHANGES", "issues": ["고칠 점", ...], "chat": "팀에게 한두 문장"}}
모든 글은 {lang}로."""


class Run:
    """한 번의 요청 처리. 이벤트는 on_event(dict)로 흘려보낸다(화면이 채팅처럼 보여 줌)."""

    def __init__(self, cfg: dict, keys: dict, task: str, on_event, mock=None):
        self.cfg, self.keys, self.task = cfg, keys, task
        self.on_event, self.mock = on_event, mock
        self.ws = Workspace(cfg["workspace"])
        self.calls = 0
        self.usage = {}
        self.state = "running"       # running → waiting_approval | failed | done | discarded
        self.stop_flag = threading.Event()

    # ---------- 공통 ----------
    def emit(self, role: str, text: str, kind: str = "chat", **extra):
        self.on_event({"time": dt.datetime.now().strftime("%H:%M:%S"), "role": role, "kind": kind, "text": text, **extra})

    def ask(self, role: str, system: str, user: str) -> dict:
        if self.stop_flag.is_set():
            raise RuntimeError("사용자가 멈췄습니다")
        if self.calls >= int(self.cfg.get("max_calls", 12)):
            raise RuntimeError(f"AI 호출 한도({self.cfg.get('max_calls', 12)}회)에 닿았습니다")
        rc = self.cfg["roles"][role]
        prov = rc["provider"]
        self.calls += 1
        text, use = providers.chat(prov, rc.get("model", ""), self.keys.get(prov, ""), system, user,
                                   json_mode=True, mock=self.mock)
        u = self.usage.setdefault(role, {"calls": 0, "in": 0, "out": 0, "provider": prov, "model": rc.get("model", "")})
        u["calls"] += 1; u["in"] += use.get("in", 0); u["out"] += use.get("out", 0)
        try:
            return providers.parse_json(text)
        except (ValueError, json.JSONDecodeError):
            raise RuntimeError(f"{role} 답을 JSON으로 읽지 못했습니다: {text[:300]}")

    # ---------- 단계 ----------
    def plan(self) -> dict:
        files = self.ws.files()
        lang = self.cfg.get("lang", "한국어")
        system = PLANNER.format(max_tasks=self.cfg.get("max_tasks", 3), lang=lang)
        readme = ""
        for name in ("README.md", "readme.md", "CLAUDE.md", "AGENTS.md"):
            if name in files:
                readme += self.ws.bundle([name], budget=8000) + "\n"
        user = f"# 요청\n{self.task}\n\n# 저장소 파일 목록 ({len(files)}개)\n" + "\n".join(files[:1500]) + \
               (f"\n\n# 참고 문서\n{readme}" if readme else "")
        p = self.ask("planner", system, user)
        tasks = p.get("tasks") or []
        if not tasks:
            raise RuntimeError("기획 담당이 작업을 만들지 못했습니다")
        p["tasks"] = tasks[: int(self.cfg.get("max_tasks", 3))]
        return p

    def build(self, t: dict, extra: str = "") -> dict:
        lang = self.cfg.get("lang", "한국어")
        system = BUILDER.format(lang=lang)
        user = (f"# 전체 요청\n{self.task}\n\n# 이번 작업\n{t.get('title')}\n{t.get('instructions')}\n\n"
                f"# 관련 파일 현재 내용\n{self.ws.bundle(t.get('files') or [])}")
        if extra:
            user += f"\n\n# 반드시 반영할 리뷰·테스트 결과\n{extra}"
        r = self.ask("builder", system, user)
        edits = r.get("edits") or []
        changed = self.ws.apply(edits)
        r["changed"] = changed
        return r

    def review(self, plan: dict, test_ok: bool, test_out: str) -> dict:
        lang = self.cfg.get("lang", "한국어")
        diff = self.ws.diff()
        if len(diff) > 60000:
            diff = diff[:60000] + "\n... (diff 60,000자 넘어 생략)"
        user = (f"# 요청\n{self.task}\n\n# 계획\n{json.dumps(plan, ensure_ascii=False)}\n\n# 변경 diff\n{diff or '(변경 없음)'}\n\n"
                f"# 테스트\n{'통과' if test_ok else '실패'}\n{test_out[-3000:]}")
        return self.ask("reviewer", REVIEWER.format(lang=lang), user)

    # ---------- 전체 흐름 ----------
    def run(self):
        try:
            self.emit("system", f"작업 폴더: {self.ws.root}", "info")
            plan = self.plan()
            self.emit("planner", plan.get("chat") or plan.get("summary", ""), "chat")
            self.emit("planner", "\n".join(f"{i}. {t.get('title')}" for i, t in enumerate(plan["tasks"], 1)), "plan")

            for i, t in enumerate(plan["tasks"], 1):
                r = self.build(t)
                self.emit("builder", f"작업 {i}: {r.get('chat') or r.get('note', '')}", "chat",
                          files=r.get("changed", []))

            rounds = int(self.cfg.get("max_fix_rounds", 2))
            for rnd in range(rounds + 1):
                ok, out = self.ws.run_test(self.cfg.get("test_command", ""))
                self.emit("tester", ("테스트 통과" if ok else "테스트 실패") + f"\n{out[-1500:]}", "test", ok=ok)
                rv = self.review(plan, ok, out)
                verdict = (rv.get("verdict") or "").upper()
                issues = rv.get("issues") or []
                self.emit("reviewer", (rv.get("chat") or verdict) + ("\n- " + "\n- ".join(issues) if issues else ""),
                          "review", verdict=verdict)
                if verdict == "APPROVE" and ok:
                    break
                if rnd == rounds:
                    self.emit("system", f"수정 라운드 {rounds}번을 다 썼습니다. 변경 내용을 보고 직접 판단해 주세요.", "info")
                    break
                fix = {"title": f"수정 라운드 {rnd + 1}", "instructions": "리뷰와 테스트 결과를 반영해 고친다.",
                       "files": sorted(set(self.ws.changed()) | {f for t in plan["tasks"] for f in (t.get('files') or [])})}
                extra = "리뷰: " + "; ".join(issues) + f"\n테스트 {'통과' if ok else '실패'}:\n{out[-2000:]}"
                r = self.build(fix, extra)
                self.emit("builder", f"수정 라운드 {rnd + 1}: {r.get('chat') or r.get('note', '')}", "chat",
                          files=r.get("changed", []))

            self.state = "waiting_approval"
            self.emit("system", "사람 확인 차례입니다. 변경 내용을 보고 승인하거나 버려 주세요.", "approval",
                      changed=self.ws.changed(), usage=self.usage)
        except (providers.ProviderError, WorkspaceError, RuntimeError) as e:
            self.state = "failed"
            self.emit("system", f"멈췄습니다: {e}", "error", changed=self.ws.changed())
        except Exception as e:   # 예상 못 한 오류도 화면에 보여 준다
            self.state = "failed"
            self.emit("system", f"예상 못 한 오류: {type(e).__name__}: {e}", "error", changed=self.ws.changed())

    def approve(self) -> str:
        branch = "ai-office/" + dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        msg = f"AI Office: {self.task[:60]}"
        res = self.ws.commit_to_branch(branch, msg) if self.ws.changed() else "바뀐 파일이 없습니다."
        self.state = "done"
        self.emit("system", res, "info")
        return res

    def discard(self) -> str:
        self.ws.discard()
        self.state = "discarded"
        self.emit("system", "변경을 모두 되돌렸습니다.", "info")
        return "discarded"
