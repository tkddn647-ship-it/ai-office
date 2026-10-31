"""AI 제공자 호출. 표준 라이브러리(urllib)만 써서 exe 크기를 줄인다.

사용자가 넣은 키로 각 회사 API를 직접 부른다. 키는 이 파일 밖으로 나가지 않고 요청 헤더에만 쓴다.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

# default_model은 키를 넣기 전 임시값이다. 키 확인 때 회사의 모델 목록에서 실제로 되는 모델로 바꾼다.
PROVIDERS = {
    "openai": {"label": "OpenAI", "default_model": "gpt-4o-mini"},
    "anthropic": {"label": "Anthropic (Claude)", "default_model": "claude-sonnet-4-5"},
    "gemini": {"label": "Google Gemini", "default_model": "gemini-flash-latest"},
    "cursor": {"label": "Cursor (Cursor CLI 필요)", "default_model": ""},
    "mock": {"label": "테스트용 가짜 AI (키 불필요)", "default_model": "mock"},
}


class ProviderError(RuntimeError):
    pass


def _get(url: str, headers: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ProviderError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise ProviderError(f"연결 실패: {e}") from None


# ---------- 모델 고르기 ----------
# 회사들이 모델을 자주 바꾸고 옛 모델을 닫는다. 이름을 고정하지 않고, 키로 '지금 쓸 수 있는 모델 목록'을 받아 고른다.
_SKIP = re.compile(r"(embed|tts|audio|realtime|transcribe|image|vision-only|search|moderation|whisper|dall|davinci|babbage|"
                   r"computer-use|live|aqa|learnlm|gemma|codex|instruct|robotics|veo|imagen|native|-exp|experimental)", re.I)


def list_models(provider: str, key: str) -> list[str]:
    """키로 그 회사가 지금 허용하는 텍스트 생성 모델 이름 목록. 키가 틀리면 여기서 401·403이 난다(비용 없음)."""
    if provider == "openai":
        d = _get("https://api.openai.com/v1/models", {"authorization": f"Bearer {key}"})
        return [m["id"] for m in d.get("data", []) if re.match(r"(gpt|o\d|chatgpt)", m.get("id", ""))]
    if provider == "anthropic":
        d = _get("https://api.anthropic.com/v1/models?limit=100", {"x-api-key": key, "anthropic-version": "2023-06-01"})
        return [m["id"] for m in d.get("data", [])]
    if provider == "gemini":
        out, tok = [], ""
        for _ in range(5):
            d = _get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200" + (f"&pageToken={tok}" if tok else ""),
                     {"x-goog-api-key": key})
            out += [m["name"].removeprefix("models/") for m in d.get("models", [])
                    if "generateContent" in (m.get("supportedGenerationMethods") or [])]
            tok = d.get("nextPageToken") or ""
            if not tok:
                break
        return out
    return []


def _ver(name: str) -> tuple:
    name = re.sub(r"-?\d{4}-?\d{2}-?\d{2}$|-\d{8}", "", name)     # 날짜 붙은 고정판은 버전으로 치지 않음
    return tuple(float(x) if "." in x else int(x) for x in re.findall(r"\d+(?:\.\d+)?", name)[:3])


def rank_models(provider: str, ids: list[str]) -> list[str]:
    """쓸 만한 순서로 정렬: 일반 대화형, 미리보기 아님, 최신 버전. 회사별로 비용 대비 무난한 급(flash·mini·sonnet)을 앞에."""
    cand = [i for i in ids if not _SKIP.search(i)]
    pref = {"gemini": r"flash(?!-lite)", "openai": r"mini", "anthropic": r"sonnet"}.get(provider, "")

    def key(i):
        dated = bool(re.search(r"\d{4}-?\d{2}-?\d{2}$|-\d{8}", i))
        return (bool(re.search(pref, i)) if pref else False, "preview" not in i and "latest" not in i, _ver(i), not dated, -len(i))
    return sorted(cand, key=key, reverse=True)


def suggested_model(err: str) -> str:
    """'models/X is no longer available … use models/Y' 같은 안내에서 Y를 꺼낸다."""
    m = re.search(r"use (?:models/)?([A-Za-z0-9._-]+)", err)
    return m.group(1).rstrip(".") if m else ""


def is_model_error(err: str) -> bool:
    return bool(re.search(r"HTTP 404|not[_ ]found|no longer available|does not exist|model_not_found|invalid model|unknown model", err, re.I))


def find_working_model(provider: str, key: str, current: str = "", tried_err: str = "") -> tuple[str, list[str]]:
    """지금 키로 실제 답이 오는 모델을 찾는다. 반환: (모델, 목록). 후보를 작은 질문으로 하나씩 시험(최대 4개)."""
    ids = list_models(provider, key)
    order = []
    for m in [suggested_model(tried_err), current] + rank_models(provider, ids):
        if m and m not in order and (not ids or m in ids or m == suggested_model(tried_err)):
            order.append(m)
    last = ""
    for m in order[:4]:
        try:
            chat(provider, m, key, "Reply with JSON only.", 'Return {"ok": true}.', timeout=40, _no_fix=True)
            return m, ids
        except ProviderError as e:
            last = str(e)
            if not is_model_error(last) and "429" not in last:
                raise
    raise ProviderError(f"쓸 수 있는 모델을 찾지 못했습니다. 마지막 오류: {last[:200]}")


def _post(url: str, headers: dict, body: dict, timeout: int) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"content-type": "application/json", **headers}, method="POST")
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            # 429·5xx만 재시도. 401·400 같은 건 키·모델 문제라 바로 알린다.
            if e.code in (429, 500, 502, 503, 529) and attempt < 2:
                last = f"HTTP {e.code}: {detail}"
                time.sleep(2 * (attempt + 1))
                continue
            raise ProviderError(f"HTTP {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            last = str(e)
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
    raise ProviderError(f"연결 실패: {last}")


IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")


AUTO_FIX = True   # 설정 '모델이 없어지면 자동으로 바꾸기'
MODEL_FIX: dict[tuple[str, str], str] = {}   # (회사, 안 되는 모델) → 대신 쓰는 모델. 설정 저장은 서버가 한다


def chat(provider: str, model: str, key: str, system: str, user: str,
         json_mode: bool = True, timeout: int = 180, mock=None, files: list[dict] | None = None, _no_fix: bool = False) -> tuple[str, dict]:
    """모델이 없어졌다는 오류가 나면 지금 쓸 수 있는 모델을 찾아 한 번 다시 묻는다 (바꾼 모델은 MODEL_FIX에 기록)."""
    model = MODEL_FIX.get((provider, model), model)
    try:
        return _chat(provider, model, key, system, user, json_mode, timeout, mock, files)
    except ProviderError as e:
        if _no_fix or provider in ("mock", "cursor") or not key or not is_model_error(str(e)):
            raise
        if not AUTO_FIX:
            raise ProviderError(f"모델 '{model}'을(를) 쓸 수 없습니다. 왼쪽 'AI 회사·모델'에서 목록 새로고침 후 다른 모델을 골라 주세요. ({str(e)[:200]})") from None
        new, _ = find_working_model(provider, key, "", str(e))
        MODEL_FIX[(provider, model)] = new
        text, usage = _chat(provider, new, key, system, user, json_mode, timeout, mock, files)
        usage["model_switched"] = f"{model} → {new}"
        return text, usage


CURSOR_INSTALL_WIN = "irm 'https://cursor.com/install?win32=true' | iex"
CURSOR_INSTALL_UNIX = "curl https://cursor.com/install -fsS | bash"


def _cursor_root():
    import os
    from pathlib import Path
    return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData/Local")) / "cursor-agent"


def find_cursor_cli() -> list[str] | None:
    """Cursor CLI(명령 이름 agent) 실행 방법. 반환: 실행할 명령 앞부분 리스트.
    Windows 설치 위치: %LOCALAPPDATA%\\cursor-agent\\agent.cmd (PATH에 없을 수 있음).
    agent.cmd는 PowerShell 5.1을 거치며 인자의 따옴표·!를 망가뜨리는 문제가 알려져 있어,
    가능하면 versions\\<버전>\\node.exe index.js 를 직접 부른다."""
    import shutil
    import sys
    from pathlib import Path
    if sys.platform == "win32":
        root = _cursor_root()
        vers = [d for d in (root / "versions").glob("*") if (d / "node.exe").is_file() and (d / "index.js").is_file()] if (root / "versions").is_dir() else []
        if vers:
            def vkey(d):
                return (tuple(int(x) for x in re.findall(r"\d+", d.name)[:4]), d.stat().st_mtime)
            d = max(vers, key=vkey)
            return [str(d / "node.exe"), str(d / "index.js")]
        for c in (root / "agent.cmd", root / "cursor-agent.cmd"):
            if c.is_file():
                return [str(c)]
    for n in ("agent", "cursor-agent"):
        p = shutil.which(n)
        if p and Path(p).stem.lower() in ("agent", "cursor-agent"):
            return [p]
    for c in (Path.home() / ".local/bin/agent", Path.home() / ".local/bin/cursor-agent"):
        if c.is_file():
            return [str(c)]
    return None


def cursor_editor_installed() -> bool:
    """Cursor 편집기(IDE)만 깔려 있는지 확인용. 편집기의 'cursor' 명령은 CLI(agent)가 아니다."""
    import os
    from pathlib import Path
    la = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData/Local"))
    return any(p.exists() for p in (la / "Programs/cursor/Cursor.exe", la / "Programs/Cursor/Cursor.exe", Path("/Applications/Cursor.app")))


def cursor_missing_message() -> str:
    import sys
    cmd = CURSOR_INSTALL_WIN if sys.platform == "win32" else CURSOR_INSTALL_UNIX
    head = ("Cursor 편집기는 깔려 있지만, 이 앱이 쓰는 Cursor CLI(명령 이름 'agent')는 따로 설치해야 합니다."
            if cursor_editor_installed() else "Cursor CLI(명령 이름 'agent')를 찾지 못했습니다.")
    return (f"{head} {'PowerShell' if sys.platform == 'win32' else '터미널'}을 열고 ① {cmd}  ② agent login 을 차례로 실행한 뒤, "
            "이 앱에서 다시 '연결 확인'을 누르세요.")


def _cursor(model: str, key: str, system: str, user: str, timeout: int) -> tuple[str, dict]:
    """Cursor CLI를 화면 없이(print 모드) 부른다. for_office의 심사 패널과 같은 방식.
    - 프롬프트는 빈 임시 폴더의 prompt.md에 쓰고 '읽고 따르라'고만 넘긴다 (Windows 명령줄 길이 한계 때문).
    - 임시 폴더 밖 파일은 건드릴 수 없고, --force를 주지 않아 셸 명령은 승인 없이 실행되지 않는다."""
    import os
    import subprocess
    import tempfile
    from pathlib import Path
    exe = find_cursor_cli()
    if not exe:
        raise ProviderError(cursor_missing_message())
    with tempfile.TemporaryDirectory(prefix="aio-cursor-") as tmp:
        (Path(tmp) / "prompt.md").write_text(f"# 지시\n{system}\n\n# 입력\n{user}\n", encoding="utf-8")
        cmd = [*exe, "-p", "Read ./prompt.md in this folder and do exactly what it says. Do not create or edit any files. "
                          "Reply with only the answer it asks for.", "--output-format", "json", "--trust"]
        if model:
            cmd += ["--model", model]
        env = {**os.environ, **({"CURSOR_API_KEY": key} if key else {}), "CURSOR_INVOKED_AS": "agent"}
        try:
            p = subprocess.run(cmd, cwd=tmp, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            raise ProviderError(f"Cursor가 {timeout}초 안에 답하지 않았습니다") from None
    out = (p.stdout or "").strip()
    try:
        d = json.loads(out[out.find("{"):])
        result = d.get("result", "")
        if p.returncode or d.get("is_error"):
            raise ProviderError(_cursor_err(p.returncode, str(result or p.stderr)))
    except (json.JSONDecodeError, ValueError):
        if p.returncode:
            raise ProviderError(_cursor_err(p.returncode, p.stderr or out)) from None
        result = out
    return result, {"in": len(system) + len(user), "out": len(result)}


def _cursor_err(code: int, msg: str) -> str:
    if re.search(r"log ?in|authenticat|unauthori|not signed|api key", msg, re.I):
        return ("Cursor CLI에 로그인돼 있지 않습니다. PowerShell에서 'agent login'을 실행해 로그인하거나, "
                f"Cursor 대시보드에서 만든 API 키를 앱의 Cursor 키 칸에 넣으세요. ({msg.strip()[-200:]})")
    return f"Cursor 오류 (exit {code}): {msg.strip()[-400:]}"


def _chat(provider: str, model: str, key: str, system: str, user: str,
          json_mode: bool = True, timeout: int = 180, mock=None, files: list[dict] | None = None) -> tuple[str, dict]:
    """한 번 묻고 답을 받는다. 반환: (텍스트, 사용량 dict).
    files: [{"name", "mime", "data"(base64)}] — 사진(png·jpg·webp·gif)과 PDF. 세 회사 모두 사진·PDF를 직접 읽는다."""
    files = files or []
    for f in files:
        if f["mime"] not in IMAGE_TYPES + ("application/pdf",):
            raise ProviderError(f"{f.get('name')}: 사진(png·jpg·webp)과 PDF만 AI에 보낼 수 있습니다")
    model = model or PROVIDERS.get(provider, {}).get("default_model", "")
    if provider == "mock":
        return (mock or demo_mock)(system, user), {"in": len(user), "out": 0}
    if provider == "cursor":
        if files:
            raise ProviderError("Cursor로는 이 앱에서 사진·PDF를 읽을 수 없습니다. '사진·PDF 읽기' 역할은 Gemini·OpenAI·Claude 중 하나로 바꿔 주세요.")
        return _cursor(model, key, system, user, max(timeout, 300))
    if not key:
        raise ProviderError(f"{PROVIDERS.get(provider, {}).get('label', provider)} API 키가 비어 있습니다. 설정에서 넣어 주세요.")

    if provider == "openai":
        content = user
        if files:
            content = [{"type": "text", "text": user}] + [
                {"type": "image_url", "image_url": {"url": f"data:{f['mime']};base64,{f['data']}"}} if f["mime"] in IMAGE_TYPES else
                {"type": "file", "file": {"filename": f.get("name") or "file.pdf", "file_data": f"data:application/pdf;base64,{f['data']}"}}
                for f in files]
        body = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}]}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        d = _post("https://api.openai.com/v1/chat/completions", {"authorization": f"Bearer {key}"}, body, timeout)
        text = d["choices"][0]["message"]["content"] or ""
        u = d.get("usage", {})
        return text, {"in": u.get("prompt_tokens", 0), "out": u.get("completion_tokens", 0)}

    if provider == "anthropic":
        content = user
        if files:
            content = [{"type": "image" if f["mime"] in IMAGE_TYPES else "document",
                        "source": {"type": "base64", "media_type": f["mime"], "data": f["data"]}} for f in files] + [{"type": "text", "text": user}]
        body = {"model": model, "max_tokens": 16000, "system": system, "messages": [{"role": "user", "content": content}]}
        d = _post("https://api.anthropic.com/v1/messages",
                  {"x-api-key": key, "anthropic-version": "2023-06-01"}, body, timeout)
        text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
        u = d.get("usage", {})
        return text, {"in": u.get("input_tokens", 0), "out": u.get("output_tokens", 0)}

    if provider == "gemini":
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}] + [
                    {"inline_data": {"mime_type": f["mime"], "data": f["data"]}} for f in files]}]}
        if json_mode:
            body["generationConfig"] = {"responseMimeType": "application/json"}
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        d = _post(url, {"x-goog-api-key": key}, body, timeout)
        cands = d.get("candidates") or []
        if not cands:
            raise ProviderError(f"Gemini 응답이 비어 있습니다: {json.dumps(d)[:300]}")
        text = "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", []))
        u = d.get("usageMetadata", {})
        return text, {"in": u.get("promptTokenCount", 0), "out": u.get("candidatesTokenCount", 0)}

    raise ProviderError(f"모르는 제공자: {provider}")


def demo_mock(system: str, user: str) -> str:
    """키 없이 흐름을 시험해 보는 가짜 AI. 작업 폴더에 AI_OFFICE_DEMO.md 한 파일만 만든다."""
    if "조치안 작성 보조" in system:
        items = [{"key": x.get("key"), "why": "가짜 AI 설명입니다.", "action": f"{x.get('fixed') or '고쳐진 버전'}으로 올리기",
                  "ticket": f"[보안] {x.get('package')} 업그레이드"} for x in json.loads(user)]
        return json.dumps({"items": items}, ensure_ascii=False)
    if "자료를 읽어 옮기는" in system:
        if "공고" in system and "questions" in system:
            return json.dumps({"company": "데모회사", "role": "SW 개발", "deadline": "2026-11-30", "defense": False,
                               "jd_text": "[데모] 담당업무: 임베디드 SW 개발\n자격요건: 관련 전공", "company_info": "",
                               "questions": [{"title": "지원동기", "text": "지원 동기를 기술하시오.", "limit": 800, "count": "with_spaces"}],
                               "unreadable": []}, ensure_ascii=False)
        if "가이드" in system:
            return json.dumps({"name": "데모 가이드", "body": "## 1. 데모 원칙 [p.1]\n- 두괄식으로 쓴다", "examples": "데모 예시 문장입니다 이것은 베끼면 안 되는 문장입니다"}, ensure_ascii=False)
        return json.dumps({"title": "데모 경험", "body": "| 항목 | 내용 |\n|---|---|\n| 기간 | [확인 필요: 기간] |\n\n## 결과\n- [확인 필요: 수치]"}, ensure_ascii=False)
    if "경험 기록을 정리해" in system:
        return json.dumps({"title": "데모 경험", "body": "# 데모 경험\n\n| 항목 | 내용 |\n|---|---|\n| 기간 | [확인 필요: 기간] |\n\n## 문제\n- "
                           + user[:200].replace("\n", " ") + "\n\n## 결과\n- [확인 필요: 수치]\n"}, ensure_ascii=False)
    if "자기소개서를 함께 쓰는" in system:
        import re as _re
        ids = sorted({int(n) for n in _re.findall(r"^Q(\d+)\.", user, _re.M)}) or [1]
        return json.dumps({"answers": [{"id": i, "text": f"[데모 소제목 {i}: 숫자 1개 포함]\n가짜 AI가 쓴 데모 문장입니다. [공고]"} for i in ids],
                           "note": "데모라 경험 기록을 읽지 않았습니다."}, ensure_ascii=False)
    if "AI가 쓴 자기소개서" in system:
        import re as _re
        ids = sorted({int(n) for n in _re.findall(r"^## Q(\d+)\.", user, _re.M)}) or [1]
        return json.dumps({"questions": [{"id": i, "ai": 1, "verdict": "사람", "sentences": [{"quote": "가짜 AI가 쓴 데모 문장입니다.", "why": "데모", "how": "데모"}], "human": []} for i in ids],
                           "overall": "데모 판별입니다."}, ensure_ascii=False)
    if "서류 심사 패널" in system:
        import re as _re
        ids = sorted({int(n) for n in _re.findall(r"^## Q(\d+)\.", user, _re.M)}) or [1]
        return json.dumps({"questions": [{"id": i, "verdict": "통과", "score": 4, "points": [], "fix": "", "ai": 1} for i in ids],
                           "overall": "데모 심사입니다.",
                           **({"requirements": [{"kind": "담당업무", "item": "데모 업무", "status": "충족", "where": "Q1", "note": ""},
                                                {"kind": "우대사항", "item": "데모 우대", "status": "없음", "where": "", "note": "근거 없음"}]}
                              if "공고 대조" in system else {})}, ensure_ascii=False)
    if "팀장" in system:
        return json.dumps({"summary": "데모: 파일 하나 만들기", "chat": "가짜 AI로 흐름만 시험합니다.",
                           "tasks": [{"title": "데모 파일 만들기", "instructions": "AI_OFFICE_DEMO.md 생성",
                                      "files": ["AI_OFFICE_DEMO.md"]}]}, ensure_ascii=False)
    if "개발 담당" in system:
        return json.dumps({"edits": [{"path": "AI_OFFICE_DEMO.md", "action": "write",
                                      "content": "# AI Office Desktop 데모\n\n가짜 AI가 만든 파일입니다. 버리기를 누르면 사라집니다.\n"}],
                           "note": "데모 파일 작성", "chat": "데모 파일을 만들었습니다."}, ensure_ascii=False)
    return json.dumps({"verdict": "APPROVE", "issues": [], "chat": "데모라 바로 승인합니다."}, ensure_ascii=False)


def parse_json(text: str) -> dict:
    """모델 답에서 JSON 객체 하나를 꺼낸다. ```json 블록이나 앞뒤 설명이 섞여도 처리."""
    t = text.strip()
    if "```" in t:
        parts = t.split("```")
        for p in parts[1::2]:
            p = p.strip()
            if p.startswith("json"):
                p = p[4:]
            try:
                return json.loads(p)
            except json.JSONDecodeError:
                continue
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        return json.loads(t[i:j + 1])
    raise ValueError("답에서 JSON을 찾지 못했습니다")
