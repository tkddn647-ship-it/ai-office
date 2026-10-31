"""실행하면 내 PC(127.0.0.1)에 화면 서버를 띄우고 브라우저를 연다. 창(콘솔)을 닫으면 종료."""
from __future__ import annotations

import sys
import threading
import webbrowser

from . import __version__
from .server import TOKEN, serve


def selftest() -> int:
    """빌드 확인용: 서버를 띄워 화면을 한 번 받아 보고 끝낸다."""
    import urllib.request
    httpd = serve(0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    html = urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/", timeout=10).read().decode("utf-8")
    httpd.shutdown()
    ok = TOKEN in html and "AI Office Desktop" in html
    print("selftest", "ok" if ok else "FAIL")
    return 0 if ok else 1


def main():
    for s in (sys.stdout, sys.stderr):   # 영어판 Windows 콘솔에서 한글 출력 때문에 죽지 않게
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:   # noqa: BLE001
            pass
    if "--selftest" in sys.argv:
        return selftest()
    httpd = serve(0)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    print(f"AI Office Desktop {__version__}")
    print(f"화면: {url}")
    print("이 창을 닫으면 프로그램이 끝납니다.")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
