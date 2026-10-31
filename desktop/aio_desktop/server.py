"""내 PC 안에서만 열리는 웹 화면(127.0.0.1). 브라우저가 화면, 이 서버가 에이전트를 돌린다.

다른 웹사이트가 이 서버를 몰래 부르지 못하게, 실행할 때마다 만든 토큰이 있어야 API가 동작한다.
"""
from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlparse

from . import settings
from .jaso import JasoRun, organize_experience
from .library import Library, LibraryError
from .orchestrator import Run
from .security import SecurityRun
from .providers import PROVIDERS
from .workspace import Workspace, WorkspaceError

TOKEN = secrets.token_urlsafe(24)


class State:
    lock = threading.Lock()
    events: list[dict] = []
    run = None          # Run | SecurityRun | JasoRun
    mode = "code"
    thread: threading.Thread | None = None


def _push(ev: dict):
    with State.lock:
        ev["i"] = len(State.events)
        State.events.append(ev)


def _page() -> bytes:
    import sys
    from pathlib import Path
    frozen = getattr(sys, "_MEIPASS", None)   # PyInstaller로 묶인 exe
    if frozen and (Path(frozen) / "aio_desktop/static/index.html").exists():
        html = (Path(frozen) / "aio_desktop/static/index.html").read_text(encoding="utf-8")
    else:
        html = resources.files("aio_desktop").joinpath("static/index.html").read_text(encoding="utf-8")
    return html.replace("__TOKEN__", TOKEN).encode("utf-8")


def _browse() -> str:
    """Windows 폴더 선택 창. tkinter가 없으면 빈 문자열."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
        path = filedialog.askdirectory(title="작업할 폴더 선택")
        root.destroy()
        return path or ""
    except Exception:   # noqa: BLE001
        return ""


def _lib() -> Library:
    return Library(settings.data_dir(settings.load_config()))


class Handler(BaseHTTPRequestHandler):
    server_version = "AIOfficeDesktop"

    def log_message(self, *a):   # 콘솔 조용히
        pass

    def _send(self, code: int, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _auth(self) -> bool:
        if self.headers.get("x-token") != TOKEN:
            self._send(403, {"error": "토큰이 맞지 않습니다"})
            return False
        return True

    def _body(self) -> dict:
        n = int(self.headers.get("content-length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    # ---------- GET ----------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self._send(200, _page(), "text/html; charset=utf-8")
        if not self._auth():
            return
        if path == "/api/config":
            cfg = settings.load_config()
            keys = settings.get_keys()
            return self._send(200, {"config": cfg, "keys_set": {p: bool(keys.get(p)) for p in PROVIDERS if p != "mock"},
                                    "providers": PROVIDERS, "key_store": settings.key_store()})
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        try:
            if path == "/api/lib":
                lib = _lib()
                return self._send(200, {"dir": str(lib.root), "experiences": lib.experiences(), "apps": lib.apps()})
            if path == "/api/exp":
                return self._send(200, {"id": q.get("id"), "body": _lib().exp_read(q.get("id", ""))})
            if path == "/api/app":
                return self._send(200, _lib().app_full(q.get("name", "")))
        except LibraryError as e:
            return self._send(400, {"error": str(e)})
        if path == "/api/events":
            since = int((urlparse(self.path).query.split("since=") + ["0"])[1].split("&")[0] or 0)
            with State.lock:
                evs = State.events[since:]
                st = State.run.state if State.run else "idle"
            return self._send(200, {"events": evs, "state": st, "mode": State.mode})
        if path == "/api/diff":
            r = State.run
            if not isinstance(r, Run):
                return self._send(200, {"diff": "", "changed": []})
            return self._send(200, {"diff": r.ws.diff(), "changed": r.ws.changed()})
        return self._send(404, {"error": "없는 주소"})

    # ---------- POST ----------
    def do_POST(self):
        path = urlparse(self.path).path
        if not self._auth():
            return
        b = self._body()
        if path == "/api/config":
            cfg = settings.load_config()
            for k in ("workspace", "test_command", "lang", "max_tasks", "max_fix_rounds", "max_calls", "data_dir"):
                if k in b:
                    cfg[k] = b[k]
            for k in ("security",):
                if isinstance(b.get(k), dict):
                    cfg[k] = {**cfg.get(k, {}), **b[k]}
            for role, v in (b.get("roles") or {}).items():
                if role in cfg["roles"] and v.get("provider") in PROVIDERS:
                    cfg["roles"][role] = {"provider": v["provider"], "model": (v.get("model") or "").strip()}
            settings.save_config(cfg)
            for prov, val in (b.get("keys") or {}).items():
                if val == "__delete__":
                    settings.set_key(prov, "")
                elif val:
                    settings.set_key(prov, val.strip())
            return self._send(200, {"ok": True})
        try:
            if path == "/api/exp/save":
                return self._send(200, {"id": _lib().exp_save(b.get("id") or None, b.get("title", ""), b.get("body", ""))})
            if path == "/api/exp/delete":
                _lib().exp_delete(b.get("id", ""))
                return self._send(200, {"ok": True})
            if path == "/api/exp/organize":
                if not (b.get("notes") or "").strip():
                    return self._send(400, {"error": "메모를 적어 주세요"})
                return self._send(200, organize_experience(settings.load_config(), settings.get_keys(), b["notes"],
                                                           mock=getattr(self.server, "mock", None)))
            if path == "/api/app/save":
                return self._send(200, {"name": _lib().app_save(b)})
        except LibraryError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:   # noqa: BLE001  AI 호출 실패 등
            return self._send(502, {"error": f"{type(e).__name__}: {e}"})
        if path == "/api/browse":
            return self._send(200, {"path": _browse()})
        if path == "/api/run":
            if State.run and State.run.state in ("running", "waiting_approval"):
                return self._send(409, {"error": "이전 작업을 먼저 승인하거나 버려 주세요"})
            mode = b.get("mode") or "code"
            cfg = settings.load_config()
            keys = settings.get_keys()
            mock = getattr(self.server, "mock", None)
            try:
                if mode == "code":
                    task = (b.get("task") or "").strip()
                    if not task:
                        return self._send(400, {"error": "요청을 적어 주세요"})
                    Workspace(cfg.get("workspace") or "")
                    first, run = task, Run(cfg, keys, task, _push, mock=mock)
                elif mode == "security":
                    first = "의존성 취약점 점검을 시작합니다."
                    run = SecurityRun(cfg, keys, cfg.get("security") or {}, _push, mock=mock,
                                      **({"fetch": self.server.fetch} if getattr(self.server, "fetch", None) else {}))
                elif mode == "jaso":
                    name = b.get("app") or ""
                    jm = "review" if b.get("jaso_mode") == "review" else "write"
                    run = JasoRun(cfg, keys, _lib(), name, _push, mock=mock, mode=jm)
                    first = f"{name} " + ("심사만 합니다 (글은 고치지 않음)." if jm == "review" else "자소서 작성을 시작합니다.")
                else:
                    return self._send(400, {"error": "모르는 모드"})
            except (WorkspaceError, LibraryError) as e:
                return self._send(400, {"error": str(e)})
            with State.lock:
                State.events = []
                State.mode = mode
            _push({"time": "", "role": "user", "kind": "chat", "text": first})
            State.run = run
            State.thread = threading.Thread(target=State.run.run, daemon=True)
            State.thread.start()
            return self._send(200, {"ok": True})
        if path == "/api/fix_task":
            r = State.run
            if not isinstance(r, SecurityRun):
                return self._send(409, {"error": "보안 점검 결과가 없습니다"})
            return self._send(200, {"task": r.fix_task()})
        if path in ("/api/approve", "/api/discard"):
            r = State.run
            if not r or r.state not in ("waiting_approval", "failed"):
                return self._send(409, {"error": "승인·버리기할 작업이 없습니다"})
            if path == "/api/approve" and (not hasattr(r, "approve") or r.state == "failed" and isinstance(r, JasoRun)):
                return self._send(409, {"error": "저장할 결과가 없습니다. 닫기를 눌러 주세요"})
            msg =r.approve() if path == "/api/approve" else r.discard()
            return self._send(200, {"ok": True, "message": msg})
        if path == "/api/stop":
            if State.run:
                State.run.stop_flag.set()
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "없는 주소"})


def serve(port: int = 0, mock=None, fetch=None) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.mock = mock
    httpd.fetch = fetch   # 시험용: 보안 모드의 공개 API 호출을 가짜로 바꿀 때
    return httpd
