"""실행하면 내 PC(127.0.0.1)에 화면 서버를 띄우고 브라우저를 연다. 창(콘솔)을 닫으면 종료."""
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
    url = f"http://127.0.0.1:{port}/#t={TOKEN}"   # 토큰은 # 뒤에만: 서버 로그·다른 사이트로 새지 않음
    print(f"AI Office Desktop {__version__}")
    print(f"화면: {url}")
    print("브라우저를 닫았으면 위 주소를 다시 여세요. 주소는 이번 실행에서만 쓸 수 있고, 남에게 보내지 마세요.")
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
