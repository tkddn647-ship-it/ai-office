"""자소서 자료 보관함. 내 PC의 '자료 폴더' 하나에 GitHub 버전(for_office)과 같은 모양으로 저장한다.

자료 폴더/
  experiences/EXP-01-제목.md        경험·활동 기록 (자소서의 유일한 근거)
  applications/<회사>-<직무>/
      app.json                      회사·직무·방산 여부·문항·사용할 경험·목표 점수
      공고.md, 회사정보.md            붙여 넣은 원문 그대로
      Q1.md ...                     근거 표시가 붙은 답
      Q1.제출용.txt ...              근거 표시를 뺀 제출용
      심사기록/날짜-시각.json          회차별 심사 결과

GitHub 버전의 experiences/ 폴더를 자료 폴더로 고르면 기존 경험 기록을 그대로 쓴다.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path

EXP_RE = re.compile(r"^(EXP-\d+)")

EXP_TEMPLATE = """# 제목

| 항목 | 내용 |
|---|---|
| 기간 | |
| 소속 / 활동 | |
| 팀 구성 | 몇 명, 누가 무엇을 |
| **내 역할** | 내가 직접 한 일 |
| 사용 기술·도구 | |

## 문제
- 무엇이 안 됐나, 왜 중요했나

## 분석 · 한 일
- 원인을 어떻게 찾았나, 무엇을 바꿨나

## 결과
- 숫자로. 시뮬레이션인지 실측인지 꼭 구분

## 못 한 것 · 지금이라면
-

## 증거
- 링크, 사진, 보고서
"""


def default_dir() -> str:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    docs = home / "Documents"
    return str((docs if docs.exists() else home) / "AIOffice자소서")


def _slug(s: str, n: int = 40) -> str:
    s = re.sub(r"[\\/:*?\"<>|\s]+", "-", s.strip()).strip("-.")
    return s[:n] or "제목없음"


class LibraryError(Exception):
    pass


class Library:
    def __init__(self, root: str):
        if not root:
            raise LibraryError("자료 폴더를 정해 주세요")
        self.root = Path(root).expanduser()
        (self.root / "experiences").mkdir(parents=True, exist_ok=True)
        (self.root / "applications").mkdir(parents=True, exist_ok=True)

    # ---------------- 경험 ----------------
    def _exp_files(self) -> list[Path]:
        return sorted(p for p in (self.root / "experiences").glob("*.md") if EXP_RE.match(p.name))

    def experiences(self) -> list[dict]:
        out = []
        for p in self._exp_files():
            txt = p.read_text(encoding="utf-8", errors="replace")
            first = next((l for l in txt.splitlines() if l.strip()), "")
            title = re.sub(r"^#\s*(EXP-\d+\s*[:：]\s*)?", "", first).strip() or p.stem
            out.append({"id": EXP_RE.match(p.name).group(1), "file": p.name, "title": title,
                        "unconfirmed": len(re.findall(r"\[확인 필요", txt)), "chars": len(txt)})
        return out

    def _exp_path(self, eid: str) -> Path:
        for p in self._exp_files():
            if EXP_RE.match(p.name).group(1) == eid:
                return p
        raise LibraryError(f"경험 {eid}가 없습니다")

    def exp_read(self, eid: str) -> str:
        return self._exp_path(eid).read_text(encoding="utf-8", errors="replace")

    def exp_save(self, eid: str | None, title: str, body: str) -> str:
        title = (title or "").strip() or "제목 없음"
        if eid:
            new = self._exp_path(eid)   # 파일 이름은 그대로 둔다 (GitHub 버전에서 링크가 깨지지 않게)
        else:
            nums = [int(EXP_RE.match(p.name).group(1)[4:]) for p in self._exp_files()]
            eid = f"EXP-{(max(nums) + 1 if nums else 1):02d}"
            new = self.root / "experiences" / f"{eid}-{_slug(title)}.md"
        body = body.strip()
        lines = body.splitlines()
        head = f"# {eid}: {title}"
        if lines and lines[0].startswith("#"):
            lines[0] = head
        else:
            lines.insert(0, head)
            lines.insert(1, "")
        new.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return eid

    def exp_delete(self, eid: str):
        p = self._exp_path(eid)
        trash = self.root / "_지운것"
        trash.mkdir(exist_ok=True)
        p.rename(trash / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{p.name}")   # 실수로 지워도 되살릴 수 있게

    def exp_bundle(self, ids: list[str] | None) -> tuple[str, set[str]]:
        exps = [e for e in self.experiences() if not ids or e["id"] in ids]
        parts = [f"=== {e['id']} ({e['file']}) ===\n{self.exp_read(e['id'])}" for e in exps]
        return "\n\n".join(parts), {e["id"] for e in exps}

    # ---------------- 지원서 ----------------
    def apps(self) -> list[dict]:
        out = []
        for d in sorted((self.root / "applications").iterdir()):
            f = d / "app.json"
            if d.is_dir() and f.exists():
                try:
                    a = json.loads(f.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    continue
                out.append({"name": d.name, "company": a.get("company", ""), "role": a.get("role", ""),
                            "status": a.get("status", ""), "updated": a.get("updated", "")})
        return sorted(out, key=lambda x: x["updated"], reverse=True)

    def _app_dir(self, name: str) -> Path:
        d = (self.root / "applications" / name).resolve()
        if d.parent != (self.root / "applications").resolve():
            raise LibraryError("잘못된 지원서 이름")
        return d

    def app_read(self, name: str) -> dict:
        d = self._app_dir(name)
        if not (d / "app.json").exists():
            raise LibraryError(f"지원서 {name}가 없습니다")
        a = json.loads((d / "app.json").read_text(encoding="utf-8"))
        a["name"] = name
        a["answers"] = {}
        for q in a.get("questions", []):
            f = d / f"Q{q['id']}.md"
            a["answers"][str(q["id"])] = f.read_text(encoding="utf-8") if f.exists() else ""
        hist = sorted((d / "심사기록").glob("*.json")) if (d / "심사기록").exists() else []
        a["last_review"] = json.loads(hist[-1].read_text(encoding="utf-8")) if hist else None
        return a

    def app_save(self, data: dict) -> str:
        company, role = (data.get("company") or "").strip(), (data.get("role") or "").strip()
        if not company:
            raise LibraryError("회사 이름을 적어 주세요")
        name = data.get("name") or _slug(f"{company}-{role}" if role else company, 60)
        d = self._app_dir(name)
        d.mkdir(parents=True, exist_ok=True)
        qs = []
        for i, q in enumerate([q for q in data.get("questions") or [] if (q.get("text") or "").strip()]):
            qs.append({"id": i + 1, "title": (q.get("title") or "").strip(), "text": q["text"].strip(),
                       "limit": int(q.get("limit") or 1000), "count": q.get("count") or "with_spaces"})
        keep = {k: data.get(k) for k in ("defense", "exp_ids", "target_ai", "target_avg", "max_rounds", "status", "deadline", "source", "imported_at")}
        a = {"company": company, "role": role, "questions": qs, **{k: v for k, v in keep.items() if v is not None},
             "updated": dt.datetime.now().isoformat(timespec="seconds")}
        (d / "app.json").write_text(json.dumps(a, ensure_ascii=False, indent=2), encoding="utf-8")
        (d / "공고.md").write_text(data.get("jd") or "", encoding="utf-8")
        (d / "회사정보.md").write_text(data.get("company_info") or "", encoding="utf-8")
        for qid, text in (data.get("answers") or {}).items():
            self.answer_save(name, int(qid), text)
        return name

    def app_full(self, name: str) -> dict:
        a = self.app_read(name)
        d = self._app_dir(name)
        a["jd"] = (d / "공고.md").read_text(encoding="utf-8") if (d / "공고.md").exists() else ""
        a["company_info"] = (d / "회사정보.md").read_text(encoding="utf-8") if (d / "회사정보.md").exists() else ""
        return a

    def answer_save(self, name: str, qid: int, text: str):
        from .jaso import strip_tags
        d = self._app_dir(name)
        (d / f"Q{qid}.md").write_text(text or "", encoding="utf-8")
        (d / f"Q{qid}.제출용.txt").write_text(strip_tags(text or ""), encoding="utf-8")

    def review_save(self, name: str, rec: dict) -> Path:
        d = self._app_dir(name) / "심사기록"
        d.mkdir(exist_ok=True)
        p = d / f"{dt.datetime.now():%Y%m%d-%H%M%S}.json"
        p.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        return p
