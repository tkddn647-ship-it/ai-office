"""빌드 점검용: Windows에 설치된 Cursor CLI를 이 앱이 찾고 실행할 수 있는지. 키가 있으면 실제로 한 번 묻는다."""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from aio_desktop import providers as p  # noqa: E402

c = p.find_cursor_cli()
print("found", c)
if not c:
    sys.exit(1)
r = subprocess.run(c + ["--version"], capture_output=True, text=True, timeout=120)
print("version", r.returncode, r.stdout.strip()[:80], r.stderr.strip()[-200:])
if r.returncode:
    sys.exit(2)
key = os.environ.get("CURSOR_API_KEY", "")
if not key:
    print("CURSOR_API_KEY 없음: 실제 질문은 건너뜀")
    sys.exit(0)
text, _ = p.chat("cursor", "", key, "Reply with JSON only.", 'Return {"ok": true}.', timeout=300)
print("reply", text[:120])
sys.exit(0 if "ok" in text else 3)
