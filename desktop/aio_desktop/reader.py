"""사진·PDF·글로 넣은 자료를 AI가 읽어 공고·경험·가이드 양식으로 옮긴다.

- 사진(png·jpg·webp)과 PDF는 고른 '자료 읽기' AI에 그대로 보낸다 (세 회사 모두 직접 읽음).
- .txt·.md는 그대로, .docx는 본문 글자만 뽑아 글로 보낸다. 한글(.hwp)은 PDF로 저장해서 넣어야 한다.
- 결과는 화면의 입력 칸을 채울 뿐 저장하지 않는다. 사람이 읽고 고친 뒤 저장한다.
- 넣은 원본은 자료 폴더 _원본/날짜-종류/ 에 그대로 보관한다 (나중에 근거 확인용).
"""
from __future__ import annotations

import base64
import datetime as dt
import io
import re
import zipfile
from pathlib import Path

from . import providers
from .library import EXP_TEMPLATE, Library, _slug

MAX_FILE = 20 * 1024 * 1024
MAX_TOTAL = 30 * 1024 * 1024
TEXT_EXT = (".txt", ".md", ".csv")

COMMON = """너는 사용자가 넣은 자료를 읽어 옮기는 사람이다. 사진·PDF·글에 실제로 보이는 내용만 쓴다.
- 보이지 않거나 흐려서 못 읽은 부분은 [판독 불가]로 남긴다. 추측해서 채우지 않는다.
- 자료에 없는 사실(회사 정보, 수치, 날짜, 역할)을 덧붙이지 않는다.
- 한국어 자료는 맞춤법을 고치지 말고 글자 그대로 옮긴다."""

JD = COMMON + """
지금 자료는 채용 공고와 (있으면) 자기소개서 문항 화면이다.
- jd_text: 공고 본문을 글자 그대로 옮긴다. 요약·정리·순서 바꾸기 금지. 담당업무·자격요건·우대사항·전형 절차·마감 등 보이는 것 전부.
- questions: 자소서 문항이 보이면 원문 그대로, 글자수 제한(숫자)과 '공백 포함/제외/바이트' 표시가 보이면 그대로. 안 보이면 limit은 0, count는 "unknown".
- company, role, deadline(YYYY-MM-DD, 안 보이면 빈칸)은 자료에 보이는 대로. defense는 방위산업(무기체계·국방) 회사가 자료에서 분명할 때만 true.
답은 JSON 하나만:
{"company": "", "role": "", "deadline": "", "defense": false, "jd_text": "", "company_info": "",
 "questions": [{"title": "짧은 이름", "text": "문항 원문", "limit": 1000, "count": "with_spaces|without_spaces|bytes|unknown"}],
 "unreadable": ["못 읽은 부분 설명"]}"""

EXP = COMMON + """
지금 자료는 지원자 본인의 경험·활동 자료다 (보고서, 발표 자료, 포트폴리오, 상장, 사진, 메모 등).
아래 양식을 자료에 보이는 내용만으로 채운다.
- 기간·팀 구성·내 역할·수치·결과 중 자료에 없는 것은 [확인 필요: 무엇을]로 남긴다.
- 팀이 한 일과 본인이 한 일을 구분한다. 자료로 구분이 안 되면 [확인 필요: 내가 직접 한 부분].
- 수치는 시뮬레이션인지 실측인지 구분한다. 알 수 없으면 [확인 필요: 시뮬인지 실측인지].
- '증거' 절에 근거가 된 파일 이름과 쪽(PDF) 또는 사진 번호를 적는다.
- 마지막에 '면접 대비' 절을 만들고, 자료만으로는 답하기 어려운 꼬리질문을 3개까지 적는다.
양식:
{template}
답은 JSON 하나만: {{"title": "한 줄 제목", "body": "양식을 채운 마크다운 (제목 줄 제외)"}}"""

GUIDE = COMMON + """
지금 자료는 자기소개서 작성법 강의·책·가이드다 (슬라이드 사진이나 PDF).
- body: '쓰는 방법'만 마크다운으로 정리한다. 절은 '## 1. 제목 [p.N]' 형식, 항목은 '- '로. 쪽 번호가 보이면 [p.N]을 붙인다.
  문항 유형(지원동기·직무역량·강점약점·협업·도전·성공실패·입사 후 포부 등)별 작성법이 있으면 '## 문항별: 지원동기'처럼 유형 이름을 제목에 넣는다.
  시장 규모·회사 매출 같은 수치는 옮기지 않는다 (가이드는 내용 근거가 아니다).
- examples: 자료에 나온 자기소개서 '예시 문장'(합격작·Good/Bad 예시 원문)만 글자 그대로. 쪽마다 '## p.N' 줄로 구분. 없으면 빈칸.
  이 예시는 사용자가 베끼지 않았는지 검사하는 데만 쓴다.
답은 JSON 하나만: {"name": "가이드 이름 (짧게)", "body": "정리한 마크다운", "examples": "예시 원문"}"""


class ReadError(ValueError):
    pass


def _docx_text(b: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(b)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError):
        raise ReadError("docx 파일을 읽지 못했습니다") from None
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    return re.sub(r"<[^>]+>", "", xml).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')


def prepare(files: list[dict]) -> tuple[list[dict], str, list[dict]]:
    """화면에서 받은 [{name, mime, data(base64)}] → (AI에 보낼 사진·PDF, 글로 바꾼 내용, 원본 [{name, bytes}])."""
    send, texts, raw, total = [], [], [], 0
    for f in files or []:
        name = Path(str(f.get("name") or "file")).name[:120]
        try:
            b = base64.b64decode(f.get("data") or "", validate=True)
        except ValueError:
            raise ReadError(f"{name}: 파일을 읽지 못했습니다") from None
        if len(b) > MAX_FILE:
            raise ReadError(f"{name}: 파일 하나는 20 MB까지입니다")
        total += len(b)
        if total > MAX_TOTAL:
            raise ReadError("한 번에 30 MB까지 넣을 수 있습니다")
        raw.append({"name": name, "bytes": b})
        low = name.lower()
        mime = f.get("mime") or ""
        if mime in providers.IMAGE_TYPES or mime == "application/pdf":
            send.append({"name": name, "mime": mime, "data": base64.b64encode(b).decode()})
        elif low.endswith(TEXT_EXT) or mime.startswith("text/"):
            texts.append(f"=== {name} ===\n{b.decode('utf-8', 'replace')}")
        elif low.endswith(".docx"):
            texts.append(f"=== {name} ===\n{_docx_text(b)}")
        elif low.endswith((".hwp", ".hwpx")):
            raise ReadError(f"{name}: 한글 파일은 바로 읽을 수 없습니다. 한글에서 'PDF로 저장'한 뒤 넣어 주세요")
        else:
            raise ReadError(f"{name}: 사진(png·jpg·webp), PDF, txt·md, docx만 넣을 수 있습니다")
    return send, "\n\n".join(texts), raw


def keep_originals(lib: Library, purpose: str, raw: list[dict]) -> str:
    if not raw:
        return ""
    kind = {"jd": "공고", "exp": "경험", "guide": "가이드"}[purpose]
    d = lib.root / "_원본" / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{kind}"
    d.mkdir(parents=True, exist_ok=True)
    for r in raw:
        (d / _slug(r["name"], 100)).write_bytes(r["bytes"])
    return str(d)


def read(cfg: dict, keys: dict, lib: Library, purpose: str, files: list[dict], text: str = "", mock=None) -> dict:
    if purpose not in ("jd", "exp", "guide"):
        raise ReadError("모르는 자료 종류")
    send, ftext, raw = prepare(files)
    body = "\n\n".join(x for x in [(text or "").strip() and f"=== 직접 적은 글 ===\n{text.strip()}", ftext] if x)
    if not send and not body:
        raise ReadError("사진·파일을 넣거나 글을 적어 주세요")
    system = {"jd": JD, "exp": EXP.format(template=EXP_TEMPLATE), "guide": GUIDE}[purpose]
    user = (f"첨부 {len(send)}개 ({', '.join(f['name'] for f in send)})를 읽어 주세요.\n\n" if send else "") + (body or "")
    rc = cfg["roles"].get("reader") or cfg["roles"]["writer"]
    out, usage = providers.chat(rc["provider"], rc.get("model", ""), keys.get(rc["provider"], ""), system, user,
                                mock=mock, files=send, timeout=300)
    d = providers.parse_json(out)
    d["_saved_originals"] = keep_originals(lib, purpose, raw)
    d["_usage"] = usage
    return d


def save_guide(lib: Library, name: str, body: str, examples: str = "") -> str:
    """가이드 정리본을 guides/<이름>.md로, 예시 원문은 guides/sources/examples.txt 끝에 덧붙인다."""
    name = _slug(name or "내 가이드", 50)
    gdir = lib.root / "guides"
    (gdir / "sources").mkdir(parents=True, exist_ok=True)
    p = gdir / f"{name}.md"
    p.write_text(f"# {name}\n\n{body.strip()}\n", encoding="utf-8")
    ex = (examples or "").strip()
    if ex:
        lines = [f"## {name} {l[3:].strip()}" if l.startswith("## ") else l for l in ex.splitlines()]
        if not lines[0].startswith("## "):
            lines.insert(0, f"## {name}")
        with open(gdir / "sources" / "examples.txt", "a", encoding="utf-8") as f:
            f.write("\n" + "\n".join(lines) + "\n")
    return p.name
