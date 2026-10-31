"""GitHub 버전(for_office)에서 정리해 둔 경험·지원서를 자료 폴더로 가져온다.

가져오는 곳 세 가지 (모두 같은 규칙으로 처리)
- GitHub 저장소: 저장소 압축본(tarball) 한 번만 받는다. 비공개 저장소는 읽기 전용 토큰이 필요하다.
- 내려받은 zip (GitHub의 Code → Download ZIP)
- 이미 clone 해 둔 폴더

가져오는 것
- experiences/EXP-NN-*.md  → 그대로 (파일 이름 유지)
- applications/<폴더>/questions.yaml (+ final/<폴더>/ 또는 applications/<폴더>/의 Q*.md)
  + jd/<회사>/공고.md, 회사정보.md → 이 프로그램의 지원서 형식으로
- _template, _검사-*, README 는 건너뛴다.

안전
- 받은 압축은 디스크에 풀지 않고 메모리에서 필요한 파일만 읽는다. 링크·장치 파일은 무시, 파일 하나 2 MB, 전체 50 MB 제한.
- 파일 이름은 정해진 모양(EXP-숫자…md, Q숫자.md)만 받고, 저장 위치는 프로그램이 정한다. 압축 안의 경로를 그대로 쓰지 않는다.
- 덮어쓰기 전에 기존 파일을 자료 폴더 _백업/날짜-시각/ 에 복사한다.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import re
import shutil
import ssl
import tarfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import yaml

from .library import EXP_RE, Library, _slug

MAX_FILE = 2 * 1024 * 1024
MAX_TOTAL = 50 * 1024 * 1024
WANT = re.compile(r"^(experiences/EXP-\d+[^/]*\.md|(applications|final)/[^/_][^/]*/(questions\.yaml|Q\d+\.md)|jd/[^/_][^/]*/(공고|회사정보)\.md)$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ImportError_(Exception):
    pass


def _keep(rel: str) -> bool:
    return bool(WANT.match(rel))


# ---------------- 원본 읽기: {상대경로: bytes} ----------------
def _from_tar(data: bytes) -> dict[str, bytes]:
    out, total = {}, 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for m in tf:
            if not m.isfile():          # 링크·폴더·장치 파일 무시
                continue
            parts = m.name.split("/", 1)    # 맨 앞 '저장소-커밋' 폴더 제거
            if len(parts) < 2 or not _keep(parts[1]) or m.size > MAX_FILE:
                continue
            total += m.size
            if total > MAX_TOTAL:
                raise ImportError_("가져올 파일이 너무 큽니다 (50 MB 초과)")
            out[parts[1]] = tf.extractfile(m).read()
    return out


def _from_zip(data: bytes) -> dict[str, bytes]:
    out, total = {}, 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = [i for i in zf.infolist() if not i.is_dir()]
        top = {n.filename.split("/", 1)[0] for n in names}
        strip = len(top) == 1 and all("/" in n.filename for n in names)   # GitHub zip은 '저장소-브랜치/' 한 겹
        for i in names:
            rel = i.filename.split("/", 1)[1] if strip else i.filename
            if not _keep(rel) or i.file_size > MAX_FILE:
                continue
            total += i.file_size
            if total > MAX_TOTAL:
                raise ImportError_("가져올 파일이 너무 큽니다 (50 MB 초과)")
            out[rel] = zf.read(i)
    return out


def _from_dir(root: Path) -> dict[str, bytes]:
    out, total = {}, 0
    root = root.resolve()
    for top in ("experiences", "applications", "final", "jd"):
        base = root / top
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if p.is_symlink() or not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            if not _keep(rel) or p.stat().st_size > MAX_FILE:
                continue
            total += p.stat().st_size
            if total > MAX_TOTAL:
                raise ImportError_("가져올 파일이 너무 큽니다 (50 MB 초과)")
            out[rel] = p.read_bytes()
    return out


def fetch_github(repo: str, branch: str, token: str, opener=None) -> dict[str, bytes]:
    repo = (repo or "").strip().removeprefix("https://github.com/").strip("/").removesuffix(".git")
    if not REPO_RE.match(repo):
        raise ImportError_("저장소는 '아이디/저장소이름' 형식으로 적어 주세요 (예: tkddn647-ship-it/for_office)")
    ref = urllib.parse.quote((branch or "main").strip(), safe="")
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/tarball/{ref}",
                                 headers={"accept": "application/vnd.github+json", "user-agent": "AIOfficeDesktop",
                                          "x-github-api-version": "2022-11-28",
                                          **({"authorization": f"Bearer {token.strip()}"} if token else {})})
    try:
        if opener:
            data = opener(req)
        else:
            with urllib.request.urlopen(req, timeout=60, context=ssl.create_default_context()) as r:
                data = r.read(MAX_TOTAL * 2 + 1)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            raise ImportError_(f"GitHub에서 거절했습니다 ({e.code}). 비공개 저장소면 토큰이 필요하고, 토큰에 이 저장소 'Contents 읽기' 권한이 있어야 합니다. 저장소 이름·브랜치도 확인하세요.") from None
        raise ImportError_(f"GitHub 오류 {e.code}") from None
    if len(data) > MAX_TOTAL * 2:
        raise ImportError_("저장소 압축본이 너무 큽니다")
    return _from_tar(data)


def read_source(kind: str, value: str, token: str = "", branch: str = "main", opener=None) -> dict[str, bytes]:
    if kind == "github":
        return fetch_github(value, branch, token, opener)
    p = Path(value or "").expanduser()
    if kind == "zip":
        if not p.is_file():
            raise ImportError_("zip 파일을 찾을 수 없습니다")
        return _from_zip(p.read_bytes())
    if kind == "folder":
        if not p.is_dir():
            raise ImportError_("폴더를 찾을 수 없습니다")
        return _from_dir(p)
    raise ImportError_("모르는 가져오기 방식")


# ---------------- 자료 폴더에 반영 ----------------
def _txt(b: bytes) -> str:
    return b.decode("utf-8", errors="replace").replace("\r\n", "\n")


def apply(lib: Library, files: dict[str, bytes], what: tuple[str, ...] = ("exp", "app"), source: str = "") -> dict:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = lib.root / "_백업" / stamp
    rep = {"exp_new": [], "exp_updated": [], "exp_same": 0, "app_new": [], "app_updated": [], "app_same": 0,
           "skipped": [], "backup": ""}

    def back(p: Path):
        dst = backup / p.relative_to(lib.root)
        dst.parent.mkdir(parents=True, exist_ok=True)
        (shutil.copytree if p.is_dir() else shutil.copy2)(p, dst)
        rep["backup"] = str(backup)

    if "exp" in what:
        local = {EXP_RE.match(p.name).group(1): p for p in lib._exp_files()}
        for rel, b in sorted(files.items()):
            if not rel.startswith("experiences/"):
                continue
            name = rel.split("/")[-1]
            if "/" in name or not EXP_RE.match(name) or not name.endswith(".md"):
                continue
            eid = EXP_RE.match(name).group(1)
            dst = lib.root / "experiences" / name
            old = local.get(eid)
            text = _txt(b)
            if old and old.read_text(encoding="utf-8", errors="replace") == text and old.name == name:
                rep["exp_same"] += 1
                continue
            if old:
                back(old)
                if old.name != name:
                    old.unlink()
                rep["exp_updated"].append(eid)
            else:
                rep["exp_new"].append(eid)
            dst.write_text(text, encoding="utf-8")

    if "app" in what:
        dirs = sorted({rel.split("/")[1] for rel in files if rel.startswith("applications/") and rel.endswith("questions.yaml")})
        for d in dirs:
            try:
                meta = yaml.safe_load(_txt(files[f"applications/{d}/questions.yaml"])) or {}
            except yaml.YAMLError as e:
                rep["skipped"].append(f"{d}: questions.yaml 읽기 실패 ({str(e).splitlines()[0]})")
                continue
            if not isinstance(meta, dict) or not meta.get("questions"):
                rep["skipped"].append(f"{d}: 문항 없음")
                continue
            company = str(meta.get("company") or d)
            qs = []
            for q in meta.get("questions") or []:
                if not isinstance(q, dict) or not q.get("text"):
                    continue
                qs.append({"id": int(q.get("id") or len(qs) + 1), "title": str(q.get("title") or ""), "text": str(q["text"]),
                           "limit": int(q.get("limit") or 1000), "count": q.get("count") or "with_spaces"})
            src = "final" if any(r.startswith(f"final/{d}/Q") for r in files) else "applications"
            answers = {str(q["id"]): _txt(files[f"{src}/{d}/Q{q['id']}.md"]) for q in qs if f"{src}/{d}/Q{q['id']}.md" in files}
            jd = files.get(f"jd/{company}/공고.md") or files.get(f"final/{d}/공고.md")
            info = files.get(f"jd/{company}/회사정보.md") or files.get(f"final/{d}/회사정보.md")
            data = {"name": _slug(d, 60), "company": company, "role": str(meta.get("role") or ""),
                    "deadline": str(meta.get("deadline") or ""), "defense": bool(meta.get("defense")),
                    "status": str(meta.get("result") or ""), "questions": qs,
                    "jd": _txt(jd) if jd else "", "company_info": _txt(info) if info else "", "answers": answers,
                    "source": source, "imported_at": stamp}
            ad = lib.root / "applications" / data["name"]
            if (ad / "app.json").exists():
                cur = lib.app_full(data["name"])
                same = (cur.get("questions") == qs and cur.get("jd", "") == data["jd"] and cur.get("company_info", "") == data["company_info"]
                        and all(cur["answers"].get(k, "") == v for k, v in answers.items()) and cur.get("status", "") == data["status"])
                if same:
                    rep["app_same"] += 1
                    continue
                back(ad)
                keep = json.loads((ad / "app.json").read_text(encoding="utf-8"))
                for k in ("exp_ids", "target_ai", "target_avg", "max_rounds"):   # 내가 화면에서 정한 값은 유지
                    if k in keep:
                        data[k] = keep[k]
                rep["app_updated"].append(data["name"])
            else:
                rep["app_new"].append(data["name"])
            lib.app_save(data)
    return rep


def summary_text(rep: dict) -> str:
    L = [f"경험: 새로 {len(rep['exp_new'])}개, 바뀜 {len(rep['exp_updated'])}개, 같음 {rep['exp_same']}개",
         f"지원서: 새로 {len(rep['app_new'])}개, 바뀜 {len(rep['app_updated'])}개, 같음 {rep['app_same']}개"]
    if rep["skipped"]:
        L.append("건너뜀: " + "; ".join(rep["skipped"]))
    if rep["backup"]:
        L.append(f"바뀌기 전 파일은 {rep['backup']}에 백업했습니다.")
    return "\n".join(L)
