"""내 PC 안에서만 열리는 웹 화면(127.0.0.1). 브라우저가 화면, 이 서버가 에이전트를 돌린다.

보안
- 127.0.0.1에만 열어 다른 PC에서는 접속할 수 없다.
- 실행할 때마다 새 토큰을 만들고, 토큰은 브라우저 주소의 # 뒤로만 넘긴다(서버에 다시 보내지지 않는 부분).
  화면 HTML에는 토큰이 없어서, 같은 PC의 다른 프로그램이 화면을 받아 가도 API를 쓸 수 없다.
- Host 헤더가 127.0.0.1/localhost가 아니면 거절한다 (DNS 리바인딩 공격 차단).
- 모든 API는 x-token 헤더가 있어야 한다. 다른 웹사이트는 이 헤더를 붙여 요청할 수 없다 (브라우저 CORS 사전 확인에서 막힘).
- 요청 크기 5 MB 제한, 화면을 다른 사이트에 끼워 넣지 못하게 막는 헤더.
"""
from __future__ import annotations

import hmac
import json
from datetime import datetime
from pathlib import Path
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlparse

from . import settings
from .jaso import JasoRun, organize_experience
from .library import Library, LibraryError
from .orchestrator import Run
from .importer import ImportError_, apply as import_apply, read_source, summary_text
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
    synced = False      # 켤 때 자동 가져오기는 한 번만
    import_lock = threading.Lock()


def _push(ev: dict):
    with State.lock:
        ev["i"] = len(State.events)
        State.events.append(ev)


def _static(name: str) -> bytes:
    import sys
    from pathlib import Path
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen and (Path(frozen) / "aio_desktop/static" / name).exists():
        return (Path(frozen) / "aio_desktop/static" / name).read_bytes()
    return resources.files("aio_desktop").joinpath("static/" + name).read_bytes()


def _page() -> bytes:
    import sys
    from pathlib import Path
    frozen = getattr(sys, "_MEIPASS", None)   # PyInstaller로 묶인 exe
    if frozen and (Path(frozen) / "aio_desktop/static/index.html").exists():
        html = (Path(frozen) / "aio_desktop/static/index.html").read_text(encoding="utf-8")
    else:
        html = resources.files("aio_desktop").joinpath("static/index.html").read_text(encoding="utf-8")
    return html.encode("utf-8")


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
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("referrer-policy", "no-referrer")
        self.send_header("content-security-policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                                    "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        if (self.headers.get("host") or "").lower() not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self._send(403, {"error": "허용되지 않은 주소"})
            return False
        return True

    def _auth(self) -> bool:
        if not hmac.compare_digest((self.headers.get("x-token") or "").encode(), TOKEN.encode()):
            self._send(403, {"error": "토큰이 맞지 않습니다. 검은 창에 나온 주소로 다시 열어 주세요"})
            return False
        return True

    def _body(self) -> dict:
        n = int(self.headers.get("content-length") or 0)
        limit = 45 if urlparse(self.path).path == "/api/read" else 5   # 사진·PDF 올리기만 크게 (base64라 원본보다 1.33배)
        if n > limit * 1024 * 1024:
            raise ValueError("요청이 너무 큽니다")
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _import(self, b: dict):
        """GitHub 버전에서 경험·지원서 가져오기. startup=True는 화면이 켜질 때 부르는 자동 가져오기
        (설정에서 auto를 켰을 때만, 실행마다 한 번)."""
        cfg = settings.load_config()
        sync = cfg.get("sync") or {}
        if b.get("startup"):
            if State.synced or not sync.get("auto") or sync.get("kind") != "github" or not sync.get("value"):
                return self._send(200, {"not_run": True})
            State.synced = True
        else:
            for k in ("kind", "value", "branch", "auto"):
                if k in b:
                    sync[k] = b[k]
            cfg["sync"] = sync
            settings.save_config(cfg)
        if not State.import_lock.acquire(blocking=False):
            return self._send(409, {"error": "이미 가져오는 중입니다"})
        try:
            token = settings.get_keys().get("github", "")
            files = read_source(sync.get("kind", "github"), sync.get("value", ""), token, sync.get("branch") or "main",
                                opener=getattr(self.server, "gh_opener", None))
            rep = import_apply(_lib(), files, tuple(b.get("what") or ("exp", "app", "guide")), source=sync.get("kind", ""))
            rep["text"] = summary_text(rep)
            return self._send(200, rep)
        except (ImportError_, LibraryError, OSError) as e:
            return self._send(400, {"error": str(e)})
        finally:
            State.import_lock.release()

    # ---------- GET ----------
    def do_GET(self):
        path = urlparse(self.path).path
        if not self._host_ok():
            return
        if path == "/":
            return self._send(200, _page(), "text/html; charset=utf-8")
        if path == "/icon.png":
            return self._send(200, _static("icon.png"), "image/png")
        if not self._auth():
            return
        if path == "/api/config":
            cfg = settings.load_config()
            keys = settings.get_keys()
            return self._send(200, {"config": cfg, "keys_set": {p: bool(keys.get(p)) for p in settings.SECRET_NAMES},
                                    "providers": PROVIDERS, "key_store": settings.key_store()})
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        try:
            if path == "/api/lib":
                lib = _lib()
                from .guides import Guides
                return self._send(200, {"dir": str(lib.root), "experiences": lib.experiences(), "apps": lib.apps(),
                                        "guides": Guides(lib.root).summary()})
            if path == "/api/exp":
                return self._send(200, {"id": q.get("id"), "body": _lib().exp_read(q.get("id", ""))})
            if path == "/api/guides":
                lib = _lib()
                gd = lib.root / "guides"
                items = [{"name": p.name, "chars": p.stat().st_size} for p in sorted(gd.glob("*.md"))] if gd.is_dir() else []
                return self._send(200, {"guides": items})
            if path == "/api/guide":
                lib = _lib()
                name = Path(q.get("name", "")).name
                p = lib.root / "guides" / name
                if not name.endswith(".md") or not p.is_file():
                    return self._send(400, {"error": "없는 가이드"})
                return self._send(200, {"name": name, "body": p.read_text(encoding="utf-8", errors="replace")})
            if path == "/api/app":
                from .guides import Guides
                lib = _lib()
                a = lib.app_full(q.get("name", ""))
                g = Guides(lib.root)
                a["guide_plan"] = {str(k): v for k, v in g.plan(a.get("questions") or [], bool(a.get("defense")))["per_q"].items()}
                a["guides"] = g.summary()
                return self._send(200, a)
        except LibraryError as e:
            return self._send(400, {"error": str(e)})
        if path == "/api/ping":
            return self._send(200, {"ok": True})
        if path == "/api/events":
            lt = getattr(self.server, "lifetime", None)
            if lt:
                lt.seen()
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
        if not self._host_ok():
            return
        if path == "/api/bye":          # 창을 닫을 때 브라우저가 보내는 신호 (sendBeacon은 헤더를 못 붙여 본문에 토큰)
            n = min(int(self.headers.get("content-length") or 0), 200)
            tok = self.rfile.read(n).decode("utf-8", "replace").strip()
            lt = getattr(self.server, "lifetime", None)
            if lt and hmac.compare_digest(tok.encode(), TOKEN.encode()):
                lt.bye()
            return self._send(204, b"", "text/plain")
        if not self._auth():
            return
        try:
            b = self._body()
        except ValueError as e:
            return self._send(413, {"error": str(e)})
        if path == "/api/config":
            cfg = settings.load_config()
            for k in ("workspace", "test_command", "test_auto", "lang", "max_tasks", "max_fix_rounds", "max_calls", "data_dir"):
                if k in b:
                    cfg[k] = b[k]
            for k in ("security", "sync", "ui"):
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
            if path == "/api/read":
                from .reader import read
                return self._send(200, read(settings.load_config(), settings.get_keys(), _lib(), b.get("purpose", ""),
                                            b.get("files") or [], b.get("text") or "", mock=getattr(self.server, "mock", None)))
            if path == "/api/guide/save":
                from .reader import save_guide
                if not (b.get("body") or "").strip():
                    return self._send(400, {"error": "가이드 내용이 비어 있습니다"})
                return self._send(200, {"name": save_guide(_lib(), b.get("name", ""), b["body"], b.get("examples", ""))})
            if path == "/api/guide/delete":
                lib = _lib()
                name = Path(str(b.get("name") or "")).name
                p = lib.root / "guides" / name
                if not name.endswith(".md") or not p.is_file():
                    return self._send(400, {"error": "없는 가이드"})
                trash = lib.root / "_지운것"
                trash.mkdir(exist_ok=True)
                p.rename(trash / f"{datetime.now():%Y%m%d-%H%M%S}-{name}")
                return self._send(200, {"ok": True})
            if path == "/api/keytest":
                # 키 확인 + 지금 쓸 수 있는 모델 찾기. 이 회사를 쓰는 역할 중 안 되는 모델은 찾은 모델로 바꾼다.
                from . import providers as pv
                prov = b.get("provider", "")
                if prov not in pv.PROVIDERS:
                    return self._send(400, {"error": "모르는 AI"})
                if prov == "mock":
                    return self._send(200, {"ok": True, "model": "mock", "models": ["mock"], "changed": []})
                if prov == "cursor":   # 모델 목록 대신 짧은 질문 하나로 확인. 모델은 비우면 Cursor 기본 모델
                    if not pv.find_cursor_cli():
                        return self._send(400, {"error": "Cursor CLI(cursor-agent)가 이 PC에 없습니다. https://cursor.com/cli 에서 설치하세요."})
                    pv.chat("cursor", "", settings.get_keys().get("cursor", ""), "Reply with JSON only.", 'Return {"ok": true}.', timeout=120)
                    return self._send(200, {"ok": True, "model": "(Cursor 기본 모델)", "models": [], "changed": []})
                key = settings.get_keys().get(prov, "")
                if not key:
                    return self._send(400, {"error": "키가 비어 있습니다"})
                cfg = settings.load_config()
                cur = next((v.get("model", "") for v in cfg["roles"].values() if v.get("provider") == prov), "")
                model, ids = pv.find_working_model(prov, key, cur)
                ranked = pv.rank_models(prov, ids)
                changed = []
                for r, v in cfg["roles"].items():
                    if v.get("provider") == prov and (not v.get("model") or (ids and v["model"] not in ids) or v["model"] == cur and cur != model):
                        changed.append(f"{r}: {v.get('model') or '(없음)'} → {model}")
                        v["model"] = model
                settings.save_config(cfg)
                return self._send(200, {"ok": True, "model": model, "models": ranked[:40], "changed": changed})
            if path == "/api/exp/organize":
                if not (b.get("notes") or "").strip():
                    return self._send(400, {"error": "메모를 적어 주세요"})
                return self._send(200, organize_experience(settings.load_config(), settings.get_keys(), b["notes"],
                                                           mock=getattr(self.server, "mock", None)))
            if path == "/api/app/save":
                return self._send(200, {"name": _lib().app_save(b)})
        except LibraryError as e:
            return self._send(400, {"error": str(e)})
        except ValueError as e:   # 읽기 실패(ReadError 포함)·JSON 아님
            return self._send(400, {"error": str(e)})
        except Exception as e:   # noqa: BLE001  AI 호출 실패 등
            return self._send(502, {"error": f"{type(e).__name__}: {e}"})
        if path == "/api/import":
            return self._import(b)
        if path == "/api/browse_file":
            return self._send(200, {"path": _browse_file()})
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
                    jm = {"review": "screen"}.get(b.get("jaso_mode"), b.get("jaso_mode")) or "write"
                    if jm not in ("write", "screen", "aicheck"):
                        return self._send(400, {"error": "모르는 자소서 작업"})
                    run = JasoRun(cfg, keys, _lib(), name, _push, mock=mock, mode=jm)
                    first = f"{name} " + {"write": "자소서 작성을 시작합니다. 회차마다 서류 심사와 AI 작성 확인을 함께 합니다.",
                                          "screen": "공고 기준 서류 심사를 합니다 (글은 고치지 않음).",
                                          "aicheck": "AI가 쓴 글처럼 보이는지 확인합니다 (글은 고치지 않음)."}[jm]
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
        if path == "/api/quit":
            lt = getattr(self.server, "lifetime", None)
            if State.run and State.run.state == "running":
                return self._send(409, {"error": "AI 작업이 돌고 있습니다. 멈춤을 누른 뒤 종료하세요"})
            if lt:
                lt.quit.set()
            return self._send(200, {"ok": True})
        if path == "/api/stop":
            if State.run:
                State.run.stop_flag.set()
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "없는 주소"})


def _browse_file() -> str:
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
        path = filedialog.askopenfilename(title="GitHub에서 받은 zip 선택", filetypes=[("zip", "*.zip")])
        root.destroy()
        return path or ""
    except Exception:   # noqa: BLE001
        return ""


def serve(port: int = 0, mock=None, fetch=None) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.mock = mock
    httpd.fetch = fetch   # 시험용: 보안 모드의 공개 API 호출을 가짜로 바꿀 때
    return httpd
