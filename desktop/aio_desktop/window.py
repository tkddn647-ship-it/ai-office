"""앱 창 띄우기와 수명 관리.

- Windows 10·11에 기본으로 깔린 Microsoft Edge(없으면 Chrome)의 '앱 모드'로 연다: 주소창·탭 없는 전용 창.
  이 프로그램 전용 프로필 폴더를 써서 평소 쓰는 브라우저의 로그인·확장 프로그램과 섞이지 않는다.
- 창을 닫으면 프로그램도 끝난다. 단, AI 작업이 돌고 있으면 끝날 때까지 기다렸다가 끝낸다.
- 이미 켜져 있으면 새로 띄우지 않고 기존 프로그램의 창을 하나 더 연다.
- Edge·Chrome을 못 찾으면 기본 브라우저 탭으로 연다 (그때는 탭을 닫고 3분 뒤 끝남).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import settings

IDLE_EXIT = 180      # 화면이 이만큼(초) 아무 요청도 없으면 끝냄 (창이 최소화돼도 브라우저가 1분에 한 번은 부름)
BYE_GRACE = 8        # 창을 닫은 뒤 새로고침인지 기다리는 시간


def find_browser() -> str | None:
    if os.environ.get("AIO_BROWSER"):          # 시험·특수 환경용
        return os.environ["AIO_BROWSER"]
    if sys.platform != "win32":
        for n in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
            p = shutil.which(n)
            if p:
                return p
        return None
    cands = [shutil.which("msedge"), shutil.which("chrome")]
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            cands += [str(Path(base) / "Microsoft/Edge/Application/msedge.exe"), str(Path(base) / "Google/Chrome/Application/chrome.exe")]
    return next((c for c in cands if c and Path(c).is_file()), None)


def open_window(url: str, extra: list[str] | None = None) -> subprocess.Popen | None:
    exe = find_browser()
    if not exe:
        webbrowser.open(url)
        return None
    prof = settings.app_dir() / "window"
    args = [exe, f"--app={url}", f"--user-data-dir={prof}", "--no-first-run", "--no-default-browser-check",
            "--window-size=1440,920", "--disable-features=Translate,msEdgeSidebarV2", "--disable-sync", "--disable-background-networking", *(extra or [])]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.Popen(args, creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        webbrowser.open(url)
        return None


# ---------- 하나만 켜기 ----------
def _inst_file() -> Path:
    return settings.app_dir() / "instance.json"


def existing_url() -> str | None:
    """이미 켜진 프로그램이 있으면 그 주소. 토큰은 사용자 폴더(%APPDATA%)에만 둔다."""
    f = _inst_file()
    if not f.exists():
        return None
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        req = urllib.request.Request(f"http://127.0.0.1:{d['port']}/api/ping", headers={"x-token": d["token"]})
        with urllib.request.urlopen(req, timeout=2) as r:
            if r.status == 200:
                return f"http://127.0.0.1:{d['port']}/#t={d['token']}"
    except Exception:   # noqa: BLE001  죽은 기록
        pass
    return None


def register(port: int, token: str):
    f = _inst_file()
    f.write_text(json.dumps({"port": port, "token": token, "pid": os.getpid()}), encoding="utf-8")
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass


def unregister():
    try:
        d = json.loads(_inst_file().read_text(encoding="utf-8"))
        if d.get("pid") == os.getpid():
            _inst_file().unlink()
    except Exception:   # noqa: BLE001
        pass


# ---------- 언제 끝낼지 ----------
class Lifetime:
    """창 프로세스가 끝나거나, 화면 요청이 오래 끊기거나, 사용자가 종료를 누르면 서버를 멈춘다."""

    def __init__(self, httpd, busy):
        self.httpd, self.busy = httpd, busy
        self.last_seen = 0.0
        self.bye_at = 0.0
        self.quit = threading.Event()
        threading.Thread(target=self._watch, daemon=True).start()

    def seen(self):
        self.last_seen = time.time()
        self.bye_at = 0.0

    def bye(self):
        self.bye_at = time.time()

    def follow(self, proc: subprocess.Popen | None):
        if proc is None:
            return
        def wait():
            t0 = time.time()
            proc.wait()
            if time.time() - t0 < 5:      # 이미 떠 있던 브라우저에 넘기고 바로 끝난 경우 → 요청 감시로만 판단
                return
            self.bye()
        threading.Thread(target=wait, daemon=True).start()

    def _watch(self):
        while not self.quit.is_set():
            time.sleep(1)
            now = time.time()
            idle = self.last_seen and now - self.last_seen > IDLE_EXIT
            closed = self.bye_at and now - self.bye_at > BYE_GRACE and self.last_seen < self.bye_at
            if (idle or closed) and not self.busy():
                self.quit.set()
        threading.Thread(target=self.httpd.shutdown, daemon=True).start()
