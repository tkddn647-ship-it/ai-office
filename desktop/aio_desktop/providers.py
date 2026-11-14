"""AI 제공자 호출. 표준 라이브러리(urllib)만 써서 exe 크기를 줄인다.

사용자가 넣은 키로 각 회사 API를 직접 부른다. 키는 이 파일 밖으로 나가지 않고 요청 헤더에만 쓴다.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

PROVIDERS = {
    "openai": {"label": "OpenAI", "default_model": "gpt-4o-mini"},
    "anthropic": {"label": "Anthropic (Claude)", "default_model": "claude-sonnet-4-5"},
    "gemini": {"label": "Google Gemini", "default_model": "gemini-2.5-flash"},
    "codex": {"label": "ChatGPT 구독 (Codex CLI 로그인, 키 불필요)", "default_model": "", "no_key": True},
    "mock": {"label": "테스트용 가짜 AI (키 불필요)", "default_model": "mock", "no_key": True},
}


def needs_key(provider: str) -> bool:
    return not PROVIDERS.get(provider, {}).get("no_key")


class ProviderError(RuntimeError):
    pass


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


def chat(provider: str, model: str, key: str, system: str, user: str,
         json_mode: bool = True, timeout: int = 180, mock=None) -> tuple[str, dict]:
    """한 번 묻고 답을 받는다. 반환: (텍스트, 사용량 dict)."""
    model = model or PROVIDERS.get(provider, {}).get("default_model", "")
    if provider == "mock":
        return (mock or demo_mock)(system, user), {"in": len(user), "out": 0}
    if provider == "codex":
        return codex_chat(model, system, user, json_mode=json_mode, timeout=timeout)
    if not key:
        raise ProviderError(f"{PROVIDERS.get(provider, {}).get('label', provider)} API 키가 비어 있습니다. 설정에서 넣어 주세요.")

    if provider == "openai":
        body = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        d = _post("https://api.openai.com/v1/chat/completions", {"authorization": f"Bearer {key}"}, body, timeout)
        text = d["choices"][0]["message"]["content"] or ""
        u = d.get("usage", {})
        return text, {"in": u.get("prompt_tokens", 0), "out": u.get("completion_tokens", 0)}

    if provider == "anthropic":
        body = {"model": model, "max_tokens": 16000, "system": system, "messages": [{"role": "user", "content": user}]}
        d = _post("https://api.anthropic.com/v1/messages",
                  {"x-api-key": key, "anthropic-version": "2023-06-01"}, body, timeout)
        text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
        u = d.get("usage", {})
        return text, {"in": u.get("input_tokens", 0), "out": u.get("output_tokens", 0)}

    if provider == "gemini":
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}]}
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


CODEX_LOGIN_HINT = ("Codex CLI를 설치하고 ChatGPT 계정으로 로그인해 주세요: "
                    "npm install -g @openai/codex  →  codex login (\"Sign in with ChatGPT\" 선택)")


def parse_codex_jsonl(out: str) -> tuple[str, dict, str | None]:
    """codex exec --json 출력(JSON Lines)에서 마지막 답, 사용량, 오류를 꺼낸다."""
    text, usage, err = "", {}, None
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = ev.get("type", "")
        item = ev.get("item") or {}
        if kind == "item.completed" and item.get("type") == "agent_message":
            text = item.get("text", "") or text
        elif kind == "turn.completed":
            u = ev.get("usage") or {}
            usage = {"in": u.get("input_tokens", 0) or 0, "out": u.get("output_tokens", 0) or 0}
        elif kind in ("turn.failed", "error"):
            e = ev.get("error") or {}
            err = (e.get("message") if isinstance(e, dict) else str(e)) or ev.get("message") or line[:300]
    return text, usage, err


def codex_chat(model: str, system: str, user: str, json_mode: bool = True, timeout: int = 180,
               runner=subprocess.run) -> tuple[str, dict]:
    """내 PC의 Codex CLI(codex exec)로 묻는다. 인증은 사용자가 'codex login'으로 한 ChatGPT 로그인을 그대로 쓴다.
    키를 이 앱이 받지 않는다. 빈 임시 폴더에서 읽기 전용 샌드박스로 돌려 글(JSON)만 받는다
    (파일 수정은 이 앱이 사람 승인 후 직접 한다)."""
    exe = shutil.which("codex")
    if not exe:
        raise ProviderError("Codex CLI(codex)를 찾지 못했습니다. " + CODEX_LOGIN_HINT)
    prompt = f"{system}\n\n{user}"
    if json_mode:
        prompt += "\n\n답은 JSON 객체 하나만 출력하라. 파일을 만들거나 고치지 말고 답만 하라."
    cmd = [exe, "exec", "--json", "--skip-git-repo-check", "--sandbox", "read-only", "--ephemeral"]
    if model:
        cmd += ["-m", model]
    cmd.append("-")
    with tempfile.TemporaryDirectory(prefix="aio-codex-") as cwd:
        try:
            p = runner(cmd, cwd=cwd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ProviderError(f"Codex가 {timeout}초 안에 답하지 않았습니다") from None
    text, usage, err = parse_codex_jsonl(p.stdout or "")
    if p.returncode != 0 or err or not text:
        msg = err or (p.stderr or p.stdout or "")[-500:]
        low = msg.lower()
        if any(w in low for w in ("login", "auth", "401", "unauthorized", "not logged")):
            msg += "  →  " + CODEX_LOGIN_HINT
        if any(w in low for w in ("usage limit", "rate limit", "429")):
            msg += "  →  ChatGPT 요금제 사용 한도에 닿았을 수 있습니다. 잠시 뒤 다시 하거나 다른 제공자로 바꿔 주세요."
        raise ProviderError(f"Codex 실패 (exit {p.returncode}): {msg}")
    return text, usage


def demo_mock(system: str, user: str) -> str:
    """키 없이 흐름을 시험해 보는 가짜 AI. 작업 폴더에 AI_OFFICE_DEMO.md 한 파일만 만든다."""
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
