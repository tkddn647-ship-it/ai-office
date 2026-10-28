#!/usr/bin/env python3
"""AI Office 오케스트레이터: 이슈 → 기획(Planner) → 구현(Builder) → 리뷰(Reviewer) → PR.

사용법:
  # GitHub Actions에서 (워크플로가 환경변수를 넣어줌)
  python office/orchestrator.py

  # 로컬 테스트: 커밋/푸시/PR 없이 기획만 확인
  python office/orchestrator.py --issue-title "로그인 버튼 색 변경" --issue-body "..." --plan-only

  # 로컬 테스트: 구현·리뷰까지 하되 push/PR은 안 함
  python office/orchestrator.py --issue-title "..." --issue-body "..." --no-push
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML이 필요합니다: pip install pyyaml")

OFFICE_DIR = Path(__file__).resolve().parent          # AI Office 본체 (중앙 레포 ai-office)
REPO_ROOT = Path(os.environ.get("AI_OFFICE_REPO_ROOT") or OFFICE_DIR.parent).resolve()
# 레포별 설정(config.yaml)·역할 덮어쓰기(roles/*.md). 워크플로가 시작할 때 임시 폴더로 복사해 두므로
# 작업 브랜치로 바꿔도(그 브랜치에 office/가 없어도) 그대로 쓸 수 있다.
CONFIG_DIR = Path(os.environ.get("AI_OFFICE_CONFIG_DIR") or REPO_ROOT / "office").resolve()
LOG_DIR = Path(os.environ.get("AI_OFFICE_LOG_DIR") or REPO_ROOT / ".ai-office-logs")


def role_prompt(name: str) -> str:
    """레포의 office/roles/<name>.md 가 있으면 그것, 없으면 본체 기본값."""
    for d in (CONFIG_DIR / "roles", OFFICE_DIR / "roles"):
        if (d / f"{name}.md").exists():
            return (d / f"{name}.md").read_text(encoding="utf-8")
    raise FileNotFoundError(f"역할 지시문 {name}.md 가 없습니다")


def load_config() -> dict:
    return yaml.safe_load((CONFIG_DIR / "config.yaml").read_text(encoding="utf-8")) or {}


# ---------- 유틸 ----------

def log(msg: str) -> None:
    print(f"[ai-office {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def fill(template: str, **values) -> str:
    """{key}만 치환. 이슈 본문의 중괄호 때문에 str.format이 깨지는 걸 피한다."""
    for k, v in values.items():
        template = template.replace("{" + k + "}", str(v))
    return template


def sh(cmd: list[str], check: bool = True, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True, check=check, **kw)


def save_log(name: str, content: str) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    (LOG_DIR / name).write_text(content, encoding="utf-8")


# ---------- 엔진 호출 ----------
# 리눅스는 명령줄 인자 하나가 128 KB를 넘으면 실행이 안 된다 (한글은 글자당 3바이트).
# 긴 프롬프트는 짧은 안내만 인자로 주고 본문은 표준입력으로 넘긴다.
ARG_LIMIT = 100_000
STDIN_NOTE = "표준입력으로 받은 지시문 전체를 그대로 따르라."


def _prompt_args(prompt: str) -> tuple[str, str | None]:
    if len(prompt.encode("utf-8")) <= ARG_LIMIT:
        return prompt, None
    return STDIN_NOTE, prompt


def run_claude(prompt: str, role: dict) -> str:
    """Claude Code CLI. 인증: CLAUDE_CODE_OAUTH_TOKEN(구독) 또는 ANTHROPIC_API_KEY."""
    if not (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")):
        raise RuntimeError("claude: 인증 정보가 비어 있습니다. 레포 Settings → Secrets → Actions에 CLAUDE_CODE_OAUTH_TOKEN이 있는지, "
                           "다른 계정의 본체를 부른다면 워크플로에서 secrets를 이름으로 넘기는지 확인하세요 (secrets: inherit는 같은 조직만 전달됨)")
    arg, stdin = _prompt_args(prompt)
    cmd = ["claude", "-p", arg, "--output-format", "json", "--max-turns", str(role.get("max_turns", 8))]
    if role.get("model"):
        cmd += ["--model", role["model"]]
    if role.get("allowed_tools"):
        cmd += ["--allowedTools", ",".join(role["allowed_tools"])]
    return _run_engine(cmd, role, "claude", stdin)


def run_cursor(prompt: str, role: dict) -> str:
    """Cursor Agent CLI. 인증: CURSOR_API_KEY."""
    arg, stdin = _prompt_args(prompt)
    cmd = ["cursor-agent", "-p", arg, "--output-format", "json"]
    if role.get("model"):
        cmd += ["--model", role["model"]]
    cmd += role.get("extra_args", [])
    return _run_engine(cmd, role, "cursor", stdin)


GEMINI_URL = os.environ.get("GEMINI_API_URL") or \
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_DEFAULT_MODEL = "gemini-flash-latest"


def run_gemini(prompt: str, role: dict) -> str:
    """Gemini API를 직접 호출 (REST). 인증: GEMINI_API_KEY (AI Studio 키, 무료·유료 모두).
    Gemini CLI는 2026-06-18부터 무료 등급·AI Pro/Ultra 구독 요청을 받지 않아 쓰지 않는다.
    도구(파일 읽기·수정)가 없으므로 글만 내는 역할(리뷰어·기획)용. 프롬프트에 diff가 들어 있다."""
    import urllib.error
    import urllib.request
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("gemini: GEMINI_API_KEY 가 없습니다 (레포 Secrets에 등록)")
    model = role.get("model") or GEMINI_DEFAULT_MODEL
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
    timeout = role.get("timeout_sec", 900)
    data = None
    for attempt in range(2):   # 503(일시 과부하)만 한 번 더. 429(한도)는 바로 대체 엔진으로
        req = urllib.request.Request(GEMINI_URL.format(model=model), data=body, method="POST",
                                     headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                msg = json.loads(raw)["error"]["message"].split(" For more information")[0]
            except Exception:  # noqa: BLE001
                msg = raw[:300]
            if e.code in (500, 503) and attempt == 0:
                time.sleep(20)
                continue
            raise RuntimeError(f"gemini 실패 (HTTP {e.code}, 모델 {model}): {msg[:500]}")
        except (urllib.error.URLError, OSError) as e:
            raise RuntimeError(f"gemini 연결 실패: {type(e).__name__}: {e}")
    cands = (data or {}).get("candidates") or []
    if not cands:
        raise RuntimeError(f"gemini 응답 없음: {json.dumps((data or {}).get('promptFeedback', data), ensure_ascii=False)[:300]}")
    text = "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", []))
    u = data.get("usageMetadata") or {}
    usage = {k: v for k, v in {"input_tokens": u.get("promptTokenCount"),
                               "output_tokens": u.get("candidatesTokenCount"),
                               "cache_read_input_tokens": u.get("cachedContentTokenCount")}.items() if v}
    _last_meta.clear()
    _last_meta.update(usage=usage, cost=None, turns=None)
    log(f"  gemini({model}) 완료 · usage={usage}")
    if cands[0].get("finishReason") == "MAX_TOKENS":
        text += "\n\n> ⚠ Gemini 출력 길이 한도로 잘림"
    return text


def _run_engine(cmd: list[str], role: dict, engine: str, stdin: str | None = None) -> str:
    timeout = role.get("timeout_sec", 900)
    try:
        p = sh(cmd, check=False, timeout=timeout, input=stdin)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{engine}: {timeout}초 안에 끝나지 않았습니다")
    except FileNotFoundError:
        raise RuntimeError(f"{cmd[0]} 명령을 찾을 수 없습니다. 설치/PATH를 확인하세요")

    out = p.stdout.strip()
    try:
        data = json.loads(out[out.find("{"):]) if "{" in out else json.loads(out)
    except json.JSONDecodeError:
        if p.returncode != 0:
            raise RuntimeError(f"{engine} 실패 (exit {p.returncode}):\n{(p.stderr or out)[-2000:]}")
        return out  # 일부 버전은 JSON 대신 텍스트

    if p.returncode != 0 or data.get("is_error"):
        raise RuntimeError(
            f"{engine} 실패 (exit {p.returncode}, subtype={data.get('subtype')}):\n"
            f"{data.get('result') or p.stderr[-2000:]}"
        )
    usage = data.get("usage") or {}  # Cursor CLI는 토큰 수를 안 줄 수 있음 → 표에 "-"
    _last_meta.clear()
    _last_meta.update(usage=usage, cost=data.get("total_cost_usd"), turns=data.get("num_turns"))
    log(f"  {engine} 완료 · usage={usage} · cost={data.get('total_cost_usd')}")
    return data.get("result", "")


ENGINES = {"claude": run_claude, "cursor": run_cursor, "gemini": run_gemini}
_last_meta: dict = {}
USAGE: list[dict] = []   # 이번 실행의 호출별 사용량 → PR에 표로


def engines_needed(cfg: dict) -> set[str]:
    out = set()
    for r in (cfg.get("roles") or {}).values():
        out.add(r.get("engine"))
        if r.get("fallback_engine"):
            out.add(r["fallback_engine"])
    return out - {None}


def check_policy(cfg: dict) -> None:
    """gemini_allowed: false 인 레포(개인정보)에서 Gemini를 쓰려 하면 시작부터 막는다.
    (Gemini 무료 등급은 약관상 사람 검토자가 입력·출력을 볼 수 있고, 개인정보 전송 금지)"""
    if cfg.get("gemini_allowed", True) is False and "gemini" in engines_needed(cfg):
        sys.exit("이 레포는 gemini_allowed: false 인데 config.yaml에 gemini 역할이 있습니다. 개인정보 보호를 위해 중단합니다.")
    b = (cfg.get("roles") or {}).get("builder") or {}
    if "gemini" in (b.get("engine"), b.get("fallback_engine")):
        sys.exit("builder에 gemini는 쓸 수 없습니다 (파일을 고치는 도구가 없음). builder는 claude 또는 cursor로.")


def call(role_name: str, prompt: str, cfg: dict) -> str:
    role = cfg["roles"][role_name]
    chain = [role["engine"]] + ([role["fallback_engine"]] if role.get("fallback_engine") else [])
    last_err = None
    for i, engine in enumerate(chain):
        if engine not in ENGINES:
            raise ValueError(f"{role_name}.engine '{engine}'은 지원하지 않습니다 ({'|'.join(ENGINES)})")
        if i == 0:
            sub = dict(role)
        else:
            # 대체 엔진엔 원래 엔진 전용 값(model, extra_args 등)을 넘기지 않고, fallback: {...}의 값만 덧씌운다
            sub = {k: v for k, v in role.items() if k in ("max_turns", "allowed_tools", "timeout_sec")}
            sub.update(role.get("fallback") or {})
            log(f"  {chain[i - 1]} 실패 → {engine}로 대신 실행")
        log(f"{role_name} → {engine} 호출")
        _last_meta.clear()
        t0, ok = time.time(), False
        try:
            result = ENGINES[engine](prompt, sub)
            ok = True
        except RuntimeError as e:
            last_err = e
            continue
        finally:
            USAGE.append({"role": role_name + (" (대체)" if i else ""), "engine": engine, "ok": ok,
                          "secs": round(time.time() - t0), "usage": dict(_last_meta.get("usage") or {}),
                          "cost": _last_meta.get("cost"), "turns": _last_meta.get("turns")})
        save_log(f"{role_name}.out.md", result)
        return result
    raise last_err


def _n(v) -> str:
    return f"{v:,}" if isinstance(v, (int, float)) and v else "-"


def format_usage() -> str:
    """호출별 사용량 표. 구독 사용 시 cost는 API로 환산한 참고값일 수 있다 (실제 청구 아님)."""
    if not USAGE:
        return "(호출 없음)"
    lines = ["| 역할 | 엔진 | 입력 토큰 | 캐시 읽기 | 출력 토큰 | 턴 | 시간 |", "|---|---|---|---|---|---|---|"]
    tot = {"claude": 0, "cursor": 0, "gemini": 0}
    for u in USAGE:
        us = u["usage"]
        tot[u["engine"]] = tot.get(u["engine"], 0) + 1
        lines.append(f"| {u['role']}{'' if u['ok'] else ' ❌'} | {u['engine']} | {_n(us.get('input_tokens'))} | "
                     f"{_n(us.get('cache_read_input_tokens'))} | {_n(us.get('output_tokens'))} | {_n(u['turns'])} | {u['secs']}s |")
    costs = [u["cost"] for u in USAGE if isinstance(u["cost"], (int, float))]
    lines.append(f"\n호출 수: Claude {tot.get('claude', 0)}회 · Cursor {tot.get('cursor', 0)}회 · Gemini {tot.get('gemini', 0)}회"
                 + (f" · API 환산 참고값 ${sum(costs):.3f} (구독은 이 금액이 청구되지 않음)" if costs else ""))
    lines.append("토큰이 `-`인 칸은 해당 CLI가 사용량을 알려주지 않은 것입니다.")
    save_log("usage.json", json.dumps(USAGE, ensure_ascii=False, indent=2))
    return "\n".join(lines)


# ---------- GitHub (REST만 사용: gh api) ----------

QUESTION_MARKER = "<!-- ai-office:questions -->"
GO_COMMAND = "/ai-office go"
FIX_COMMAND = "/ai-office fix"
FIX_MARKER = "<!-- ai-office:fix-round -->"


def gh_repo() -> str:
    # Actions에선 GITHUB_REPOSITORY, 로컬에선 gh가 git remote로 {owner}/{repo}를 채운다
    return os.environ.get("GITHUB_REPOSITORY") or "{owner}/{repo}"


def gh_api(path: str, *fields: str, method: str = "GET", check: bool = True):
    cmd = ["gh", "api", "-X", method, f"repos/{gh_repo()}" + (f"/{path}" if path else "")]
    for f in fields:
        cmd += ["-F" if f.split("=", 1)[1] in ("true", "false") else "-f", f]
    p = sh(cmd, check=False)
    if check and p.returncode != 0:
        raise RuntimeError(f"gh api {method} {path} 실패: {(p.stderr or p.stdout)[-800:]}")
    try:
        return json.loads(p.stdout) if p.stdout.strip() else None
    except json.JSONDecodeError:
        return None


TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}


def fetch_comments(number: int) -> list[dict]:
    """저장소 소유자·협업자와 AI Office 자신의 댓글만 쓴다.
    공개 저장소에선 아무나 댓글을 달 수 있어서, 남의 댓글로 AI에 지시를 끼워넣는 걸 막기 위함."""
    if not number:
        return []
    try:
        data = gh_api(f"issues/{number}/comments?per_page=100") or []
    except RuntimeError as e:
        log(f"  댓글을 못 가져왔습니다 (계속 진행): {e}")
        return []
    out, skipped = [], 0
    for c in data:
        body = c.get("body") or ""
        is_bot = (c.get("user") or {}).get("type") == "Bot"
        trusted = c.get("author_association") in TRUSTED
        if not (is_bot or trusted):
            skipped += 1
            continue
        out.append({"human": trusted and not is_bot and QUESTION_MARKER not in body and FIX_MARKER not in body
                    and CHAT_MARKER not in body,
                    "author": (c.get("user") or {}).get("login", "?"), "body": body})
    if skipped:
        log(f"  권한 없는 사용자의 댓글 {skipped}개는 무시합니다")
    return out


def format_comments(comments: list[dict]) -> str:
    if not comments:
        return "(댓글 없음)"
    out = []
    for c in comments[-20:]:
        who = f"사람({c['author']})" if c["human"] else "AI Office"
        out.append(f"[{who}]\n{c['body'][:2000]}")
    return "\n\n".join(out)


def question_state(comments: list[dict]) -> tuple[bool, bool]:
    """(이전에 질문을 올렸는가, 그 뒤에 사람이 답했는가 또는 /ai-office go)"""
    asked_idx = max((i for i, c in enumerate(comments) if QUESTION_MARKER in c["body"]), default=-1)
    force = any(c["human"] and GO_COMMAND in c["body"] for c in comments)
    answered = asked_idx >= 0 and any(c["human"] for c in comments[asked_idx + 1:])
    return asked_idx >= 0, answered or force


def ensure_label(name: str, color: str, desc: str) -> None:
    gh_api("labels", f"name={name}", f"color={color}", f"description={desc}", method="POST", check=False)


# ---------- 회사 채팅 (이슈 댓글로 역할별로 말한다) ----------

CHAT_MARKER = "<!-- ai-office:chat -->"
MEMORY_LABEL = "ai-office-memory"
PERSONAS = {"lead": "🧑‍💼 팀장", "dev": "👷 개발", "review": "🔍 리뷰", "test": "🧪 테스트"}
MENTIONS = {"@팀장": "lead", "@개발": "dev", "@리뷰": "review"}
MEMORY_HINT = re.compile(r"기억|앞으로|다음부터|다음엔|다음에는|항상|규칙으로")
_chat = {"n": 0, "on": False}


def chat_setup(cfg: dict, number: int, push: bool) -> None:
    _chat.update(n=number or 0, on=bool(push and number and cfg.get("chat", True)))


def say(who: str, text: str) -> None:
    """역할 이름으로 이슈에 한 줄 남긴다. 로컬 모드·chat: false면 로그에만."""
    text = (text or "").strip()
    if not text:
        return
    log(f"[{PERSONAS[who]}] {text[:200]}")
    if _chat["on"]:
        gh_api(f"issues/{_chat['n']}/comments", f"body={CHAT_MARKER}\n**{PERSONAS[who]}** {text[:3000]}",
               method="POST", check=False)


def chat_line(output: str) -> str:
    """에이전트 출력 끝의 `CHAT: ...` 한 줄 (역할 지시문이 남기라고 시킴)."""
    m = re.findall(r"^\s*CHAT:\s*(.+)$", output or "", re.M)
    return m[-1].strip() if m else ""


def memory_issue(create: bool = False) -> int | None:
    found = gh_api(f"issues?labels={MEMORY_LABEL}&state=open&per_page=1", check=False) or []
    if found:
        return found[0]["number"]
    if not create:
        return None
    ensure_label(MEMORY_LABEL, "5319e7", "AI Office: 팀 메모 (@팀장에게 남긴 규칙)")
    i = gh_api("issues", "title=🧠 AI Office 팀 메모",
               "body=이슈나 PR에서 `@팀장 앞으로 …`처럼 남긴 규칙이 여기 쌓이고, 모든 역할이 매번 읽습니다. "
               "지우고 싶은 규칙은 댓글을 지우면 됩니다.", f"labels[]={MEMORY_LABEL}", method="POST", check=False)
    return (i or {}).get("number")


_memory_cache: str | None = None


def load_memory() -> str:
    global _memory_cache
    if _memory_cache is None:
        n = memory_issue() if os.environ.get("GITHUB_ACTIONS") else None
        items = [c["body"] for c in fetch_comments(n)] if n else []
        _memory_cache = "\n".join(f"- {b.strip()[:500]}" for b in items[-30:])
    return _memory_cache


def remember(text: str, where: int) -> None:
    n = memory_issue(create=True)
    if n:
        gh_api(f"issues/{n}/comments", f"body={text} (#{where}에서)", method="POST", check=False)


# ---------- 단계 ----------

def notes(cfg: dict) -> str:
    base = (cfg.get("project_notes") or "").strip() or "(없음)"
    mem = load_memory()
    return base + (f"\n\n--- 팀 메모 (사람이 @팀장에게 남긴 규칙, 반드시 지킬 것) ---\n{mem}" if mem else "")


def extract_json(text: str) -> dict:
    """모델이 코드블록이나 설명을 섞어도 첫 JSON 객체를 뽑는다."""
    text = re.sub(r"```(?:json)?", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("Planner 출력에서 JSON을 찾지 못했습니다:\n" + text[:1500])
    return json.loads(text[start:end + 1])


def plan(issue: dict, cfg: dict) -> dict:
    tpl = role_prompt("planner")
    prompt = fill(tpl, project_notes=notes(cfg), max_tasks=cfg["max_tasks"], issue_number=issue["number"],
                  issue_title=issue["title"], issue_body=issue["body"] or "(본문 없음)",
                  issue_comments=format_comments(issue.get("comments", [])))
    result = extract_json(call("planner", prompt, cfg))
    result.setdefault("tasks", [])
    result.setdefault("questions", [])
    result.setdefault("notes", [])
    if len(result["tasks"]) > cfg["max_tasks"]:
        log(f"  작업이 {len(result['tasks'])}개라 {cfg['max_tasks']}개로 자릅니다")
        result["tasks"] = result["tasks"][: cfg["max_tasks"]]
    save_log("plan.json", json.dumps(result, ensure_ascii=False, indent=2))
    return result


def post_build(cfg: dict) -> None:
    """Builder가 끝난 직후, 커밋 전에 항상 돌리는 정리 명령 (config의 post_build_commands).
    AI가 규칙을 빼먹어도 결과물이 일정한 형식이 되도록 기계적으로 고친다. 실패해도 진행은 계속."""
    for cmd in cfg.get("post_build_commands") or []:
        p = subprocess.run(cmd, shell=True, cwd=REPO_ROOT, text=True, capture_output=True, timeout=300)
        tail = (p.stdout + p.stderr).strip().splitlines()[-1:] or [""]
        log(f"  정리: {cmd} → exit {p.returncode} {tail[0][:120]}")


def build(plan_data: dict, cfg: dict, commit: bool, issue: dict | None = None) -> list[dict]:
    tpl = role_prompt("builder")
    tasks = plan_data["tasks"]
    reports = []
    for t in tasks:
        prompt = fill(tpl, project_notes=notes(cfg), summary=plan_data.get("summary", ""), task_id=t.get("id"),
                      task_count=len(tasks), title=t.get("title", ""),
                      files=", ".join(t.get("files", [])) or "(미지정)",
                      instructions=t.get("instructions", ""), done_when=t.get("done_when", ""),
                      issue_title=(issue or {}).get("title", ""), issue_body=(issue or {}).get("body") or "(본문 없음)")
        try:
            report = call("builder", prompt, cfg)
            ok = True
        except RuntimeError as e:
            report, ok = str(e), False
            log(f"  작업 {t.get('id')} 실패: {e}")
        post_build(cfg)
        changed = bool(sh(["git", "status", "--porcelain"]).stdout.strip())
        if commit and changed:
            sh(["git", "add", "-A"])
            sh(["git", "commit", "-m", f"ai-office: task {t.get('id')} - {t.get('title', '')}"])
        reports.append({"task": t, "ok": ok, "changed": changed, "report": report})
        say("dev", f"작업 {t.get('id')} " + (chat_line(report) or ("끝냈습니다." if ok and changed else
                                                                  "바뀐 파일이 없습니다." if ok else "실패했습니다.")))
    return reports


SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", ".ai-office-logs"}


def discover_tests(cfg: dict) -> list[str]:
    """__init__.py가 있는 패키지 안의 tests.py → `python -m 패키지.tests`. 새로 만든 패키지도 자동으로 검사된다."""
    if not cfg.get("test_discover"):
        return []
    exclude = {e.strip("/") for e in cfg.get("test_exclude") or []}
    found = []
    for root, dirs, files in os.walk(REPO_ROOT):
        rel = Path(root).relative_to(REPO_ROOT)
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and (rel / d).as_posix() not in exclude)
        if "tests.py" in files and "__init__.py" in files and rel.parts:
            if all(part.isidentifier() for part in rel.parts):
                found.append(f"python -m {'.'.join(rel.parts)}.tests")
    return found


def test_command_list(cfg: dict) -> list[str]:
    cmds = list(cfg.get("test_commands") or [])
    for c in discover_tests(cfg):
        if c not in cmds:
            cmds.append(c)
    return cmds


def run_tests(cfg: dict) -> list[dict]:
    """Builder와 별개로 오케스트레이터가 직접 테스트를 돌린다 (Builder 보고를 그대로 믿지 않기 위해)."""
    results = []
    timeout = cfg.get("test_timeout_sec", 600)
    for cmd in test_command_list(cfg):
        log(f"테스트: {cmd}")
        t0 = time.time()
        try:
            p = subprocess.run(cmd, shell=True, cwd=REPO_ROOT, text=True, capture_output=True, timeout=timeout)
            code, out = p.returncode, (p.stdout + p.stderr)
        except subprocess.TimeoutExpired:
            code, out = -1, f"{timeout}초 시간 초과"
        r = {"cmd": cmd, "ok": code == 0, "code": code, "secs": round(time.time() - t0, 1),
             "tail": "\n".join(out.strip().splitlines()[-15:])}
        log(f"  {'통과' if r['ok'] else '실패'} (exit {code}, {r['secs']}s)")
        results.append(r)
    save_log("tests.json", json.dumps(results, ensure_ascii=False, indent=2))
    return results


def format_tests(tests: list[dict], detail: bool = True) -> str:
    if not tests:
        return "(실행한 테스트 없음 — config.yaml의 test_commands / test_discover)"
    lines = ["| 명령 | 결과 | 시간 |", "|---|---|---|"]
    for t in tests:
        result = "✅ 통과" if t["ok"] else "❌ 실패 (exit %s)" % t["code"]
        lines.append(f"| `{t['cmd']}` | {result} | {t['secs']}s |")
    if detail:
        for t in tests:
            lines.append(f"\n<details><summary>{t['cmd']} 출력 (끝 15줄)</summary>\n\n```\n{t['tail']}\n```\n</details>")
    return "\n".join(lines)


def format_reports(reports: list[dict]) -> str:
    out = []
    for r in reports:
        state = "실패" if not r["ok"] else ("변경 있음" if r["changed"] else "변경 없음")
        out.append(f"[작업 {r['task'].get('id')}: {r['task'].get('title', '')} — {state}]\n{r['report'][:2500]}")
    return "\n\n".join(out) or "(없음)"


def review(issue: dict, plan_data: dict, base: str, reports: list[dict], tests: list[dict], cfg: dict) -> str:
    """base: diff 기준 (브랜치 이름 또는 origin/브랜치)."""
    diff = sh(["git", "diff", f"{base}...HEAD", "--", ".", ":!.ai-office-logs"], check=False).stdout
    if not diff.strip():  # 커밋 안 하는 로컬 모드: 새 파일도 diff에 보이게 intent-to-add
        sh(["git", "add", "-A", "-N"], check=False)
        diff = sh(["git", "diff", "--", ".", ":!.ai-office-logs"], check=False).stdout
    if not diff.strip():
        return "VERDICT: REQUEST_CHANGES\n## 판정 이유\n변경 사항이 없습니다."
    limit = cfg["max_diff_chars"]
    note = f" (앞 {limit}자만 표시, 전체 {len(diff)}자)" if len(diff) > limit else ""
    tpl = role_prompt("reviewer")
    prompt = fill(tpl, project_notes=notes(cfg), issue_number=issue["number"], issue_title=issue["title"],
                  issue_body=issue["body"] or "", plan_json=json.dumps(plan_data, ensure_ascii=False, indent=2),
                  builder_reports=format_reports(reports), test_results=format_tests(tests),
                  truncated_note=note, diff=diff[:limit])
    try:
        return call("reviewer", prompt, cfg)
    except RuntimeError as e:
        return f"VERDICT: REQUEST_CHANGES\n## 판정 이유\n리뷰 실패 — 사람이 직접 확인하세요.\n\n```\n{e}\n```"


def parse_verdict(review_md: str) -> str:
    m = re.search(r"VERDICT:\s*(APPROVE|REQUEST_CHANGES)", review_md)
    if m:
        return m.group(1)
    head = review_md[:400]  # 형식을 안 지켰을 때 대비
    if "REQUEST_CHANGES" in head:
        return "REQUEST_CHANGES"
    if "APPROVE" in head:
        return "APPROVE"
    return "UNKNOWN"


def status_banner(verdict: str, reports: list[dict], tests: list[dict]) -> tuple[bool, str]:
    problems = []
    if verdict == "REQUEST_CHANGES":
        problems.append("리뷰어가 **수정 요청**")
    elif verdict == "UNKNOWN":
        problems.append("리뷰 판정을 **읽지 못함**")
    failed_tests = [t for t in tests if not t["ok"]]
    if failed_tests:
        problems.append(f"CI 테스트 **{len(failed_tests)}/{len(tests)}개 실패**")
    failed_tasks = [r for r in reports if not r["ok"]]
    if failed_tasks:
        problems.append(f"작업 **{len(failed_tasks)}/{len(reports)}개 실패**")
    if problems:
        return True, ("> [!WARNING]\n> **사람 확인 필요:** " + " · ".join(problems) +
                      "\n> 아래 리뷰와 테스트 결과를 보고 판단하세요. 이대로 머지해도 되지만, 그건 사람의 결정입니다.")
    tests_note = f"CI 테스트 {len(tests)}개 통과" if tests else "CI 테스트 미설정"
    return False, f"> [!NOTE]\n> 리뷰 **APPROVE** · {tests_note}. 그래도 머지 전에 diff는 한 번 보세요."


def pr_body(issue: dict, plan_data: dict, reports: list[dict], tests: list[dict],
            review_md: str, banner: str, base: str) -> str:
    n = issue["number"]
    lines = [banner, ""]
    if n:
        lines.append(f"Closes #{n}" if base == default_branch() else
                     f"이슈 #{n} (작업 브랜치 `{base}`로 머지돼도 이슈는 자동으로 닫히지 않으니 직접 닫아 주세요)")
    lines += ["", f"**요약:** {plan_data.get('summary', '')}"]
    if plan_data.get("auto_rounds"):
        lines.append(f"**자동 수정:** 테스트·리뷰 결과를 보고 {plan_data['auto_rounds']}번 스스로 고쳤습니다.")
    lines += ["", "### 작업 결과"]
    for r in reports:
        mark = "✅" if r["ok"] and r["changed"] else ("⚠️ 변경 없음" if r["ok"] else "❌ 실패")
        lines.append(f"- {mark} **{r['task'].get('id')}. {r['task'].get('title', '')}**")
        lines.append("  <details><summary>Builder 보고</summary>\n\n" + r["report"][:3000] + "\n\n  </details>")
    lines += ["", "### CI 테스트 (오케스트레이터가 직접 실행)", format_tests(tests)]
    if plan_data.get("questions"):
        lines += ["", "### 남은 질문"] + [f"- {q}" for q in plan_data["questions"]]
    if plan_data.get("notes"):
        lines += ["", "### Planner 참고 의견"] + [f"- {q}" for q in plan_data["notes"]]
    lines += ["", "### 자동 리뷰", re.sub(r"^VERDICT:.*\n?", "", review_md, count=1), "",
              "### 사용량", "<details><summary>이번 실행 사용량 (펼치기)</summary>\n\n" + format_usage() + "\n\n</details>",
              "", "---",
              "_AI Office가 만든 PR입니다. 자동 머지되지 않으니 직접 확인 후 머지하세요._"]
    return "\n".join(lines)


_default_branch_cache: str | None = None


def default_branch() -> str:
    global _default_branch_cache
    if _default_branch_cache is None:
        try:
            _default_branch_cache = (gh_api("") or {}).get("default_branch") or "main"
        except RuntimeError:
            _default_branch_cache = "main"
    return _default_branch_cache


def ask_questions(issue: dict, plan_data: dict) -> None:
    n = issue["number"]
    qs = "\n".join(f"{i}. {q}" for i, q in enumerate(plan_data["questions"], 1))
    notes_md = ("\n\n**참고 의견**\n" + "\n".join(f"- {x}" for x in plan_data["notes"])) if plan_data.get("notes") else ""
    body = (f"{QUESTION_MARKER}\n### 🤔 AI Office: 작업 전에 확인이 필요합니다\n\n"
            f"**이해한 내용:** {plan_data.get('summary', '')}\n\n"
            f"추측으로 진행하면 결과가 틀어질 수 있어서 멈췄습니다. 이 이슈에 댓글로 답해 주세요.\n\n{qs}{notes_md}\n\n"
            f"---\n**이어서 진행하려면:** 답 댓글을 단 뒤 `ai-office` 라벨을 다시 붙이세요 (방금 자동으로 뗐습니다).\n"
            f"질문을 무시하고 그냥 진행하려면 댓글에 `{GO_COMMAND}` 라고 쓰고 라벨을 붙이세요.")
    gh_api(f"issues/{n}/comments", f"body={body}", method="POST")
    gh_api(f"issues/{n}/labels/ai-office", method="DELETE", check=False)
    log(f"질문 {len(plan_data['questions'])}개를 이슈 #{n}에 올리고 멈춥니다.")


def create_pr(issue: dict, branch: str, base: str, body: str, needs_human: bool) -> str:
    title = ("[AI Office · 확인 필요] " if needs_human else "[AI Office] ") + issue["title"]
    fields = [f"title={title}", f"head={branch}", f"base={base}", f"body={body[:60000]}"]
    pr = None
    if needs_human:  # 초안(Draft)으로: 실수로 머지 버튼 누르는 것 방지
        try:
            pr = gh_api("pulls", *fields, "draft=true", method="POST")
        except RuntimeError as e:  # 무료 요금제 비공개 레포 등 Draft 미지원
            log(f"  Draft PR을 못 만들어 일반 PR로 만듭니다: {str(e)[:200]}")
    if pr is None:
        pr = gh_api("pulls", *fields, method="POST")
    if needs_human:
        set_needs_human(pr["number"], True)
    return pr.get("html_url", "")


# ---------- 수정 라운드 (/ai-office fix) ----------

def extract_section(md: str, heading: str) -> str:
    """'### heading'부터 다음 '### '나 '---' 구분선 전까지."""
    m = re.search(rf"^###[^\n]*{re.escape(heading)}[^\n]*\n(.*?)(?=^### |^---\s*$|\Z)", md, re.S | re.M)
    return m.group(1).strip() if m else ""


def set_needs_human(number: int, needs_human: bool) -> None:
    if needs_human:
        ensure_label("needs-human", "d93f0b", "AI Office: 사람 확인 필요")
        gh_api(f"issues/{number}/labels", "labels[]=needs-human", method="POST", check=False)
    else:
        gh_api(f"issues/{number}/labels/needs-human", method="DELETE", check=False)


def resolve_fix_target(number: int, push: bool) -> int | None:
    """댓글이 달린 곳이 이슈면, 그 이슈로 만들어진 가장 최근 AI Office PR을 찾는다."""
    item = gh_api(f"issues/{number}")
    if "pull_request" in item:
        return number
    prs = gh_api("pulls?state=all&per_page=100&sort=created&direction=desc", check=False) or []
    mine = [x for x in prs if x["head"]["ref"].startswith(f"ai-office/issue-{number}-")]
    if not mine:
        if push:
            gh_api(f"issues/{number}/comments", "body=AI Office: 이 이슈로 만들어진 AI Office PR을 찾지 못했습니다. "
                   "`/ai-office fix`는 고칠 **PR**에 댓글로 달아 주세요. 새 작업이면 이 이슈에 `ai-office` 라벨을 붙이세요.",
                   method="POST", check=False)
        log(f"이슈 #{number}에 연결된 AI Office PR이 없습니다.")
        return None
    target = max(mine, key=lambda x: x["number"])["number"]
    log(f"이슈 #{number}의 댓글 → 이 이슈의 PR #{target}을 대상으로 진행")
    if push:
        gh_api(f"issues/{number}/comments", f"body=AI Office: 이 이슈의 PR #{target}을 대상으로 수정 라운드를 진행합니다. "
               f"결과는 PR #{target}(또는 후속 PR)에 남깁니다.", method="POST", check=False)
    return target


def run_fix(number: int, cfg: dict, push: bool) -> int:
    pr_number = resolve_fix_target(number, push)
    if pr_number is None:
        return 0
    pr = gh_api(f"pulls/{pr_number}")
    if pr["head"]["repo"]["full_name"] != pr["base"]["repo"]["full_name"]:
        gh_api(f"issues/{pr_number}/comments", "body=AI Office: 포크에서 온 PR은 고치지 않습니다.", method="POST", check=False)
        return 1
    head, base = pr["head"]["ref"], pr["base"]["ref"]
    # 머지된 PR → 작업 브랜치에서 새 브랜치를 따서 후속 PR로. 머지 안 하고 닫힌 PR → 거부.
    followup = pr.get("state") == "closed" and bool(pr.get("merged"))
    if pr.get("state") == "closed" and not followup:
        if push:
            gh_api(f"issues/{pr_number}/comments", "body=AI Office: 머지하지 않고 닫힌 PR이라 고치지 않습니다. "
                   "다시 열거나 새 이슈로 요청해 주세요.", method="POST", check=False)
        log("닫힌(머지 안 된) PR이라 멈춥니다.")
        return 0
    comments = fetch_comments(pr_number)
    max_rounds = int(cfg.get("max_fix_rounds", 2))
    done_rounds = sum(1 for c in comments if FIX_MARKER in c["body"])
    log(f"PR #{pr_number} 수정 라운드 {done_rounds + 1}/{max_rounds} ({head} → {base})"
        + (" · 이미 머지됨 → 후속 PR" if followup else ""))
    if done_rounds >= max_rounds:
        if push:
            gh_api(f"issues/{pr_number}/comments", f"body={FIX_MARKER}\nAI Office: 수정 라운드를 이미 {max_rounds}번 했습니다 "
                   f"(`max_fix_rounds`). 더 고치려면 사람이 직접 고치거나, 새 이슈로 범위를 좁혀 주세요.", method="POST", check=False)
        log("최대 라운드에 도달해 멈춥니다.")
        return 0

    # 사람의 지시: 이번 실행을 일으킨 댓글이 우선 (이슈에 단 댓글이어도 전달됨), 없으면 PR의 최근 fix 댓글
    trigger = os.environ.get("AI_OFFICE_FIX_REQUEST") or ""
    if FIX_COMMAND in trigger:
        req = trigger.split(FIX_COMMAND, 1)[1].strip()
    else:
        req = next((c["body"].split(FIX_COMMAND, 1)[1].strip() for c in reversed(comments)
                    if c["human"] and FIX_COMMAND in c["body"]), "")
    human_request = req or "(추가 지시 없음 — 아래 리뷰 지적을 고칠 것)"
    # 가장 최근 리뷰/테스트: 리뷰가 담긴 마지막 수정 라운드 댓글, 없으면 PR 본문
    last_round = next((c["body"] for c in reversed(comments)
                       if FIX_MARKER in c["body"] and extract_section(c["body"], "자동 리뷰")), "")
    src = last_round or (pr.get("body") or "")
    latest_review = extract_section(src, "자동 리뷰") or "(리뷰 없음)"
    latest_tests = extract_section(src, "CI 테스트") or "(기록 없음)"
    m = re.search(r"(?:Closes |이슈 )#(\d+)", pr.get("body") or "")
    issue = {"number": int(m.group(1)), "title": "", "body": ""} if m else {"number": 0, "title": pr["title"], "body": ""}
    if m:
        i = gh_api(f"issues/{issue['number']}", check=False) or {}
        issue.update(title=i.get("title", ""), body=i.get("body") or "")

    sh(["git", "fetch", "origin", f"{head}:refs/remotes/origin/{head}", f"{base}:refs/remotes/origin/{base}"], check=False)
    if followup:
        work_branch = f"ai-office/fix-pr{pr_number}-{int(time.time())}"
        sh(["git", "checkout", "-B", work_branch, f"origin/{base}"])
    else:
        work_branch = head
        sh(["git", "checkout", "-B", head, f"origin/{head}"])
    if sh(["git", "status", "--porcelain"]).stdout.strip():
        sys.exit("커밋 안 된 변경이 있습니다. 먼저 커밋하거나 `git stash` 하세요.")
    if push:
        sh(["git", "config", "user.name", "ai-office[bot]"], check=False)
        sh(["git", "config", "user.email", "ai-office@users.noreply.github.com"], check=False)

    tpl = role_prompt("fixer")
    prompt = fill(tpl, project_notes=notes(cfg), issue_number=issue["number"], issue_title=issue["title"],
                  issue_body=issue["body"] or "(없음)", human_request=human_request,
                  latest_review=latest_review[:6000], latest_tests=latest_tests[:3000])
    task = {"id": f"fix{done_rounds + 1}", "title": f"수정 라운드 {done_rounds + 1}"}
    try:
        report, ok = call("builder", prompt, cfg), True
    except RuntimeError as e:
        report, ok = str(e), False
    post_build(cfg)
    changed = bool(sh(["git", "status", "--porcelain"]).stdout.strip())
    if push and changed:
        sh(["git", "add", "-A"])
        sh(["git", "commit", "-m", f"ai-office: 수정 라운드 {done_rounds + 1} (PR #{pr_number})"])
    reports = [{"task": task, "ok": ok, "changed": changed, "report": report}]

    tests = run_tests(cfg)
    plan_data = {"mode": "PR 수정 라운드 — 아래 '이전 리뷰' 지적과 사람 지시를 고쳤는지 확인할 것",
                 "human_request": human_request, "previous_review": latest_review[:4000]}
    review_md = review(issue, plan_data, f"origin/{base}", reports, tests, cfg)
    verdict = parse_verdict(review_md)
    needs_human, banner = status_banner(verdict, reports, tests)
    if not changed:
        needs_human = True
    log(f"판정: {verdict} · 변경: {changed} · 사람 확인 필요: {needs_human}")

    mark = "✅" if ok and changed else ("⚠️ 변경 없음" if ok else "❌ 실패")
    body = "\n".join([
        FIX_MARKER, f"## 🔧 수정 라운드 {done_rounds + 1}/{max_rounds}", "", banner, "",
        f"**지시:** {human_request}", "",
        f"{mark} <details><summary>Builder 보고</summary>\n\n{report[:3000]}\n\n</details>", "",
        "### CI 테스트 (오케스트레이터가 직접 실행)", format_tests(tests), "",
        "### 자동 리뷰 (PR 전체 diff 기준)", re.sub(r"^VERDICT:.*\n?", "", review_md, count=1), "",
        "### 사용량", "<details><summary>이번 라운드 사용량 (펼치기)</summary>\n\n" + format_usage() + "\n\n</details>", "",
        "---",
        ("_더 고치려면 다시 `/ai-office fix` (지시를 뒤에 덧붙일 수 있음). "
         "Draft PR이면 확인 후 **Ready for review**를 누르세요._"),
    ])
    save_log("fix_comment.md", body)
    if not push:
        log(f"로컬 모드 종료. 결과: {LOG_DIR}/fix_comment.md")
        return 0
    if followup:
        if not changed:
            gh_api(f"issues/{pr_number}/comments", f"body={body[:60000]}", method="POST")
            log("변경이 없어 후속 PR을 만들지 않았습니다.")
            return 0
        sh(["git", "push", "-u", "origin", work_branch])
        title = re.sub(r"^\[AI Office[^\]]*\]\s*", "", pr["title"])
        fu_body = (f"PR #{pr_number}(이미 머지됨)에 단 `/ai-office fix`의 **후속 수정**입니다. "
                   f"`{base}`의 최신 상태에서 이번 수정만 담았습니다.\n\n" + body)
        url = create_pr({"number": 0, "title": f"후속 수정 #{pr_number}: {title}"}, work_branch, base, fu_body, needs_human)
        gh_api(f"issues/{pr_number}/comments", f"body={FIX_MARKER}\nAI Office: 이 PR은 이미 머지돼서 수정을 **후속 PR**로 올렸습니다: {url}",
               method="POST", check=False)
        log(f"후속 PR 생성: {url}")
        return 0
    if changed:
        sh(["git", "push", "origin", f"HEAD:{head}"])
    gh_api(f"issues/{pr_number}/comments", f"body={body[:60000]}", method="POST")
    set_needs_human(pr_number, needs_human)
    title = re.sub(r"^\[AI Office[^\]]*\]\s*", "", pr["title"])
    gh_api(f"pulls/{pr_number}", f"title={'[AI Office · 확인 필요] ' if needs_human else '[AI Office] '}{title}",
           method="PATCH", check=False)
    log("수정 라운드 결과를 PR에 남겼습니다.")
    return 0


# ---------- 자동 수정 반복 (사람 없이 테스트 실패·리뷰 지적을 고침) ----------

def auto_fix(issue: dict, review_md: str, tests: list[dict], cfg: dict, commit: bool, rnd: int) -> dict:
    prompt = fill(role_prompt("fixer"), project_notes=notes(cfg), issue_number=issue["number"],
                  issue_title=issue["title"], issue_body=issue["body"] or "(없음)",
                  human_request="(자동 수정 라운드: 사람 지시 없음 — 실패한 테스트와 리뷰 지적을 고칠 것)",
                  latest_review=review_md[:6000], latest_tests=format_tests(tests)[:4000])
    task = {"id": f"auto{rnd}", "title": f"자동 수정 {rnd}"}
    try:
        report, ok = call("builder", prompt, cfg), True
    except RuntimeError as e:
        report, ok = str(e), False
    post_build(cfg)
    changed = bool(sh(["git", "status", "--porcelain"]).stdout.strip())
    if commit and changed:
        sh(["git", "add", "-A"])
        sh(["git", "commit", "-m", f"ai-office: 자동 수정 {rnd}"])
    return {"task": task, "ok": ok, "changed": changed, "report": report}


def say_tests(tests: list[dict]) -> None:
    if not tests:
        say("test", "돌릴 테스트가 설정돼 있지 않습니다.")
        return
    bad = [t for t in tests if not t["ok"]]
    say("test", f"테스트 {len(tests)}개 중 {len(tests) - len(bad)}개 통과." +
        ("" if not bad else " 실패: " + ", ".join(f"`{t['cmd']}`" for t in bad)))


def say_review(review_md: str, verdict: str) -> None:
    m = re.search(r"^#+\s*판정 이유\s*\n+(.+)$", review_md or "", re.M)
    line = chat_line(review_md) or (m.group(1).strip() if m else "")
    say("review", ("승인합니다. " if verdict == "APPROVE" else "수정이 필요합니다. ") + line)


def find_ai_pr(number: int) -> int | None:
    item = gh_api(f"issues/{number}", check=False) or {}
    if "pull_request" in item:
        return number
    prs = gh_api("pulls?state=all&per_page=100&sort=created&direction=desc", check=False) or []
    mine = [x for x in prs if x["head"]["ref"].startswith(f"ai-office/issue-{number}-")
            and (x.get("state") == "open" or x.get("merged_at"))]
    return max(mine, key=lambda x: x["number"])["number"] if mine else None


def run_chat(number: int, who: str, text: str, cfg: dict, push: bool) -> int | None:
    """@팀장/@개발/@리뷰 댓글. 규칙이면 팀 메모에 저장, PR이 있으면 수정 라운드, 없으면 None (새 작업으로 진행)."""
    chat_setup(cfg, number, push)
    if who == "lead" and MEMORY_HINT.search(text):
        if push:
            remember(text, number)
        say("lead", f"기억했습니다. 다음 작업부터 모든 역할이 이 규칙을 읽습니다: “{text[:200]}”")
        return 0
    pr = find_ai_pr(number) if push else None
    if pr:
        say("lead", f"알겠습니다. PR #{pr}에 반영하도록 개발에게 넘깁니다.")
        os.environ["AI_OFFICE_FIX_REQUEST"] = f"{FIX_COMMAND} {text}"
        return run_fix(number, cfg, push)
    say("lead", "확인했습니다. 이 이슈로 작업을 시작합니다.")
    return None


# ---------- 메인 ----------

def load_issue(args) -> dict:
    issue = None
    if args.issue_title:
        issue = {"number": args.issue_number or 0, "title": args.issue_title, "body": args.issue_body or ""}
    else:
        event_path = os.environ.get("GITHUB_EVENT_PATH")
        if event_path and Path(event_path).exists():
            ev = json.loads(Path(event_path).read_text(encoding="utf-8"))
            if "issue" in ev:
                i = ev["issue"]
                issue = {"number": i["number"], "title": i["title"], "body": i.get("body") or ""}
        num = args.issue_number or os.environ.get("ISSUE_NUMBER")
        if issue is None and num:  # workflow_dispatch
            i = gh_api(f"issues/{num}")
            issue = {"number": i["number"], "title": i["title"], "body": i.get("body") or ""}
    if issue is None:
        sys.exit("이슈 정보가 없습니다. --issue-title 또는 --issue-number를 주세요.")
    issue["comments"] = fetch_comments(issue["number"]) if not args.issue_title else []
    return issue


def run_setup(cfg: dict) -> int:
    """레포마다 다른 준비 작업 (패키지 설치, 자료 추출 등). 하나라도 실패하면 중단."""
    for cmd in cfg.get("setup_commands") or []:
        log(f"준비: {cmd}")
        p = subprocess.run(cmd, shell=True, cwd=REPO_ROOT)
        if p.returncode != 0:
            log(f"  준비 실패 (exit {p.returncode}): {cmd}")
            return p.returncode
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--issue-number", type=int)
    ap.add_argument("--issue-title")
    ap.add_argument("--issue-body")
    ap.add_argument("--plan-only", action="store_true", help="기획만 하고 종료")
    ap.add_argument("--no-push", action="store_true", help="커밋/푸시/PR 없이 로컬에서만 실행")
    ap.add_argument("--force", action="store_true", help="Planner 질문이 있어도 진행")
    ap.add_argument("--fix-pr", type=int, help="PR 수정 라운드 (/ai-office fix)")
    ap.add_argument("--needs-engine", help="워크플로용: 이 레포가 그 엔진을 쓰면 exit 0, 아니면 1")
    ap.add_argument("--hook", choices=["setup"], help="워크플로용: config의 setup_commands 실행")
    args = ap.parse_args()

    cfg = load_config()
    if args.needs_engine:
        return 0 if args.needs_engine in engines_needed(cfg) else 1
    if args.hook == "setup":
        return run_setup(cfg)
    check_policy(cfg)
    fix_pr = args.fix_pr or int(os.environ.get("AI_OFFICE_FIX_PR") or 0)  # 이슈 번호여도 됨 (PR을 찾아감)
    trigger = (os.environ.get("AI_OFFICE_FIX_REQUEST") or "").strip()
    mention = next((m for m in MENTIONS if trigger.startswith(m)), None)
    if mention and fix_pr:
        r = run_chat(fix_pr, MENTIONS[mention], trigger[len(mention):].strip(), cfg, push=not args.no_push)
        if r is not None:
            return r
        os.environ["ISSUE_NUMBER"] = str(fix_pr)   # PR이 없는 이슈 → 새 작업으로
    elif fix_pr:
        return run_fix(fix_pr, cfg, push=not args.no_push)
    issue = load_issue(args)
    log(f"이슈 #{issue['number']}: {issue['title']} (댓글 {len(issue['comments'])}개)")
    chat_setup(cfg, issue["number"], not args.no_push)

    base_branch = cfg.get("base_branch")
    if base_branch:
        cur = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
        if cur != base_branch:
            log(f"작업 브랜치로 전환: {cur} → {base_branch}")
            sh(["git", "fetch", "origin", f"{base_branch}:refs/remotes/origin/{base_branch}"], check=False)
            sh(["git", "checkout", "-B", base_branch, f"origin/{base_branch}"])

    if not args.plan_only and sh(["git", "status", "--porcelain"]).stdout.strip():
        sys.exit("커밋 안 된 변경이 있습니다. 내 작업이 AI 커밋에 섞이지 않도록 "
                 "먼저 커밋하거나 `git stash` 한 뒤 다시 실행하세요.")

    plan_data = plan(issue, cfg)
    log(f"계획: 작업 {len(plan_data['tasks'])}개 · 막는 질문 {len(plan_data['questions'])}개 · 참고 {len(plan_data['notes'])}개")
    if plan_data["tasks"] and not plan_data["questions"] and not args.plan_only:
        say("lead", f"{plan_data.get('summary', '')} 작업 {len(plan_data['tasks'])}개로 나눴습니다: " +
            " / ".join(f"{t.get('id')}. {t.get('title', '')}" for t in plan_data["tasks"]))
    if args.plan_only:
        print(json.dumps(plan_data, ensure_ascii=False, indent=2))
        return 0

    push = not args.no_push
    asked_before, answered = question_state(issue["comments"])
    proceed_anyway = args.force or answered
    if plan_data["questions"] and not proceed_anyway:
        if push and issue["number"]:
            ask_questions(issue, plan_data)
        else:
            log("막는 질문이 있어 멈춥니다 (--force로 무시 가능):\n  - " + "\n  - ".join(plan_data["questions"]))
        return 0
    if plan_data["questions"]:
        log("질문이 남았지만 사람이 답했거나 강제 진행이라 계속합니다.")
    if not plan_data["tasks"]:
        log("작업이 없습니다.")
        return 0

    base = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    branch = f"ai-office/issue-{issue['number']}-{int(time.time())}"
    if push:
        sh(["git", "config", "user.name", "ai-office[bot]"], check=False)
        sh(["git", "config", "user.email", "ai-office@users.noreply.github.com"], check=False)
        sh(["git", "checkout", "-b", branch])

    reports = build(plan_data, cfg, commit=push, issue=issue)
    tests = run_tests(cfg)
    say_tests(tests)
    review_md = review(issue, plan_data, base, reports, tests, cfg)
    verdict = parse_verdict(review_md)
    say_review(review_md, verdict)

    # 에이전트 반복: 테스트 실패나 리뷰 지적이 있으면 사람을 부르기 전에 스스로 고친다 (상한·호출 예산 안에서)
    max_auto = int(cfg.get("max_auto_rounds", 2))
    budget = int(cfg.get("max_calls", 12))
    auto_done = 0
    while auto_done < max_auto and (verdict != "APPROVE" or any(not t["ok"] for t in tests)):
        if not any(r["changed"] for r in reports):
            break
        if len(USAGE) + 2 > budget:
            say("lead", f"호출 예산 {budget}회에 가까워 자동 수정을 멈추고 사람 확인으로 넘깁니다.")
            break
        auto_done += 1
        why = "테스트 실패" if any(not t["ok"] for t in tests) else "리뷰 수정 요청"
        say("lead", f"{why}라 개발에게 다시 맡깁니다 (자동 수정 {auto_done}/{max_auto}).")
        fix = auto_fix(issue, review_md, tests, cfg, commit=push, rnd=auto_done)
        reports.append(fix)
        say("dev", chat_line(fix["report"]) or ("고쳤습니다." if fix["changed"] else "바꿀 곳을 찾지 못했습니다."))
        if not fix["changed"]:
            break
        tests = run_tests(cfg)
        say_tests(tests)
        review_md = review(issue, plan_data, base, reports, tests, cfg)
        verdict = parse_verdict(review_md)
        say_review(review_md, verdict)
    plan_data["auto_rounds"] = auto_done
    needs_human, banner = status_banner(verdict, reports, tests)
    log(f"판정: {verdict} · 사람 확인 필요: {needs_human}")
    body = pr_body(issue, plan_data, reports, tests, review_md, banner, base) if push else \
        pr_body_local(issue, plan_data, reports, tests, review_md, banner, base)
    save_log("pr_body.md", body)

    if not push:
        log(f"로컬 모드 종료. 결과: {LOG_DIR}/pr_body.md")
        return 0
    if not any(r["changed"] for r in reports):
        log("변경된 파일이 없어 PR을 만들지 않습니다.")
        if issue["number"]:
            gh_api(f"issues/{issue['number']}/comments",
                   f"body=AI Office: 변경 사항 없이 끝났습니다.\n\n{body[:60000]}", method="POST", check=False)
        return 1

    sh(["git", "push", "-u", "origin", branch])
    url = create_pr(issue, branch, base, body, needs_human)
    log(f"PR 생성: {url}")
    say("lead", f"PR 올렸습니다: {url} " + ("확인이 필요한 점이 남아 있어 초안으로 올렸습니다." if needs_human
                                            else "리뷰 승인·테스트 통과입니다.") + " 병합은 직접 해 주세요.")
    return 0


def pr_body_local(*a) -> str:
    # 로컬 모드에선 GitHub 조회(default_branch) 없이 만든다
    global _default_branch_cache
    _default_branch_cache = _default_branch_cache or "main"
    return pr_body(*a)


if __name__ == "__main__":
    sys.exit(main())
