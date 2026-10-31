"""실행하면 내 PC(127.0.0.1)에 화면 서버를 띄우고 전용 앱 창을 연다. 창을 닫으면 종료."""
from __future__ import annotations

import sys
import threading
import webbrowser

from . import __version__
from .server import TOKEN, serve


def selftest() -> int:
    """빌드 확인용: 서버를 띄워 화면을 한 번 받아 보고 끝낸다."""
    import urllib.error
    import urllib.request
    httpd = serve(0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    html = urllib.request.urlopen(base + "/", timeout=10).read().decode("utf-8")

    def code(token):
        try:
            return urllib.request.urlopen(urllib.request.Request(base + "/api/events", headers={"x-token": token}), timeout=10).status
        except urllib.error.HTTPError as e:
            return e.code
    ok = "AI Office Desktop" in html and TOKEN not in html and code(TOKEN) == 200 and code("wrong") == 403
    httpd.shutdown()
    print("selftest", "ok" if ok else "FAIL")
    return 0 if ok else 1


def _log_to_file():
    """창 모드 exe에는 콘솔이 없다. 오류를 %APPDATA%\\AIOfficeDesktop\\log.txt에 남긴다."""
    from . import settings
    if sys.stdout is None or sys.stderr is None:
        f = open(settings.app_dir() / "log.txt", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or f
        sys.stderr = sys.stderr or f


def main():
    _log_to_file()
    for s in (sys.stdout, sys.stderr):   # 영어판 Windows 콘솔에서 한글 출력 때문에 죽지 않게
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:   # noqa: BLE001
            pass
    if "--selftest" in sys.argv:
        return selftest()
    from . import window
    from .server import State
    url0 = window.existing_url()
    if url0:                                  # 이미 켜져 있으면 창만 하나 더
        window.open_window(url0)
        return 0
    httpd = serve(0)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/#t={TOKEN}"   # 토큰은 # 뒤에만: 서버 로그·다른 사이트로 새지 않음
    life = window.Lifetime(httpd, busy=lambda: bool(State.run and State.run.state == "running") or State.import_lock.locked())
    httpd.lifetime = life
    window.register(port, TOKEN)
    print(f"AI Office Desktop {__version__} · 127.0.0.1:{port}")
    smoke = "--smoketest" in sys.argv          # 빌드 확인: 앱 창이 화면을 실제로 불러오는지
    if "--browser" in sys.argv:               # 평소 브라우저 탭으로 열기
        import webbrowser
        webbrowser.open(url)
        proc = None
    else:
        proc = window.open_window(url, ["--headless=new", "--no-sandbox"] if "--headless" in sys.argv else None)
        life.follow(proc)
    result = {"ok": False}
    if smoke:
        import time

        def check():
            t0 = time.time()
            while time.time() - t0 < 60 and not life.last_seen:
                time.sleep(0.5)
            result["ok"] = bool(life.last_seen)
            print("smoketest", "ok" if result["ok"] else "FAIL (창이 화면을 불러오지 못함)", "browser:", window.find_browser())
            if proc:
                proc.terminate()
            life.quit.set()
        threading.Thread(target=check, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        window.unregister()
        httpd.server_close()
    return (0 if result["ok"] else 1) if smoke else 0


if __name__ == "__main__":
    sys.exit(main())
