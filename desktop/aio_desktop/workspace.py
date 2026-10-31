"""작업 폴더 다루기: 파일 목록·읽기, 편집 적용(폴더 밖 차단), 되돌리기, diff, git.

에이전트가 PC에서 직접 파일을 바꾸므로 안전 장치가 이 파일에 모여 있다.
- 작업 폴더 밖 경로, .git 안쪽은 읽지도 쓰지도 않는다.
- 편집 전 원본을 스냅샷으로 저장해, 사람이 '버리기'를 누르면 그대로 되돌린다.
- 셸 명령은 사용자가 설정한 테스트 명령 하나만 실행한다.
"""
from __future__ import annotations

import difflib
import fnmatch
import os
import shutil
import subprocess
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "env", "dist", "build",
             ".idea", ".vscode", ".mypy_cache", ".pytest_cache", ".next", "target", "bin", "obj"}
TEXT_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".md", ".txt", ".html", ".css", ".scss",
            ".c", ".h", ".cpp", ".hpp", ".cc", ".java", ".kt", ".go", ".rs", ".cs", ".yml", ".yaml",
            ".toml", ".ini", ".cfg", ".sh", ".bat", ".ps1", ".sql", ".xml", ".gradle", ".vue", ".svelte", ""}
MAX_FILE = 200_000


class WorkspaceError(RuntimeError):
    pass


class Workspace:
    def __init__(self, root: str):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise WorkspaceError(f"폴더가 없습니다: {self.root}")
        self.snapshot: dict[str, str | None] = {}   # 상대경로 → 원본 내용 (None = 원래 없던 파일)

    # ---------- 경로 안전 ----------
    def safe(self, rel: str) -> Path:
        rel = (rel or "").replace("\\", "/").strip()
        if rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
            raise WorkspaceError(f"절대 경로는 쓸 수 없습니다: {rel!r}")
        if not rel or rel.startswith("..") or "/../" in f"/{rel}/" or ":" in rel.split("/")[0]:
            raise WorkspaceError(f"허용되지 않는 경로: {rel!r}")
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise WorkspaceError(f"작업 폴더 밖 경로는 쓸 수 없습니다: {rel}")
        if ".git" in p.relative_to(self.root).parts:
            raise WorkspaceError(".git 폴더는 건드리지 않습니다")
        return p

    # ---------- 읽기 ----------
    def files(self, limit: int = 2000) -> list[str]:
        out = []
        ignore = self._gitignore()
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
            for f in sorted(filenames):
                p = Path(dirpath) / f
                rel = p.relative_to(self.root).as_posix()
                if any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(f, g) for g in ignore):
                    continue
                if p.suffix.lower() not in TEXT_EXT:
                    continue
                try:
                    if p.stat().st_size > MAX_FILE:
                        continue
                except OSError:
                    continue
                out.append(rel)
                if len(out) >= limit:
                    return out
        return out

    def _gitignore(self) -> list[str]:
        g = self.root / ".gitignore"
        if not g.exists():
            return []
        pats = []
        for line in g.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("!"):
                pats.append(line.rstrip("/") + ("*" if line.endswith("/") else ""))
        return pats

    def read(self, rel: str) -> str:
        p = self.safe(rel)
        if not p.is_file():
            raise WorkspaceError(f"파일이 없습니다: {rel}")
        return p.read_text(encoding="utf-8", errors="replace")

    def bundle(self, rels: list[str], budget: int = 120_000) -> str:
        """파일 여러 개를 모델에 보낼 한 덩어리 텍스트로. 예산을 넘으면 뒤 파일은 잘라 낸다."""
        parts, used = [], 0
        for rel in rels:
            try:
                txt = self.read(rel)
            except WorkspaceError:
                parts.append(f"=== {rel} (없음: 새로 만들 파일이면 write로 만드세요) ===")
                continue
            if used + len(txt) > budget:
                parts.append(f"=== {rel} (분량 초과로 생략) ===")
                continue
            used += len(txt)
            parts.append(f"=== {rel} ===\n{txt}")
        return "\n\n".join(parts)

    # ---------- 편집 ----------
    def _remember(self, rel: str, p: Path):
        if rel not in self.snapshot:
            self.snapshot[rel] = p.read_text(encoding="utf-8", errors="replace") if p.exists() else None

    def apply(self, edits: list[dict]) -> list[str]:
        """edits: [{path, action: write|replace|delete, content | find, replace}]. 바뀐 파일 목록 반환.
        하나라도 실패하면 이번 묶음에서 이미 바꾼 것까지 되돌리고 예외."""
        done, before = [], dict(self.snapshot)
        batch_orig: dict[str, str | None] = {}   # 이번 묶음 직전 상태 (실패 시 여기로 되돌림)
        try:
            for e in edits:
                rel = (e.get("path") or "").replace("\\", "/")
                act = e.get("action", "write")
                p = self.safe(rel)
                self._remember(rel, p)
                if rel not in batch_orig:
                    batch_orig[rel] = p.read_text(encoding="utf-8", errors="replace") if p.exists() else None
                if act == "write":
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(e.get("content", ""), encoding="utf-8")
                elif act == "replace":
                    if not p.is_file():
                        raise WorkspaceError(f"{rel}: 없는 파일은 replace할 수 없습니다")
                    cur = p.read_text(encoding="utf-8", errors="replace")
                    find = e.get("find", "")
                    n = cur.count(find) if find else 0
                    if n != 1:
                        raise WorkspaceError(f"{rel}: 바꿀 부분이 정확히 1번 나와야 하는데 {n}번 나옵니다")
                    p.write_text(cur.replace(find, e.get("replace", ""), 1), encoding="utf-8")
                elif act == "delete":
                    if p.is_file():
                        p.unlink()
                else:
                    raise WorkspaceError(f"모르는 action: {act}")
                done.append(rel)
        except Exception:
            for rel, original in batch_orig.items():
                self._restore_one(rel, original)
            self.snapshot = before
            raise
        return done

    def _restore_one(self, rel: str, original: str | None):
        p = self.safe(rel)
        if original is None:
            if p.exists():
                p.unlink()
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(original, encoding="utf-8")

    def discard(self):
        for rel, original in self.snapshot.items():
            self._restore_one(rel, original)
        self.snapshot = {}

    def diff(self) -> str:
        out = []
        for rel, original in sorted(self.snapshot.items()):
            p = self.safe(rel)
            new = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
            old = original or ""
            out += difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        f"a/{rel}", f"b/{rel}")
        return "".join(out)

    def changed(self) -> list[str]:
        return sorted(self.snapshot)

    # ---------- 테스트 ----------
    def run_test(self, command: str, timeout: int = 300) -> tuple[bool, str]:
        if not command.strip():
            return True, "(테스트 명령 없음)"
        try:
            env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}   # 같은 초에 바뀐 .py를 옛 캐시로 읽지 않게
            r = subprocess.run(command, cwd=self.root, shell=True, capture_output=True, text=True,
                               timeout=timeout, encoding="utf-8", errors="replace", env=env)
            out = (r.stdout + "\n" + r.stderr).strip()
            return r.returncode == 0, out[-4000:]
        except subprocess.TimeoutExpired:
            return False, f"{timeout}초 안에 끝나지 않았습니다"

    # ---------- git (있으면) ----------
    def is_git(self) -> bool:
        return (self.root / ".git").exists() and shutil.which("git") is not None

    def git(self, *args) -> tuple[int, str]:
        r = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout + r.stderr).strip()

    def commit_to_branch(self, branch: str, message: str) -> str:
        """바뀐 파일만 새 브랜치에 커밋. 원래 브랜치로 돌아오지는 않는다(사용자가 바로 확인할 수 있게)."""
        if not self.is_git():
            return "git 저장소가 아니라 파일만 바뀐 상태로 둡니다."
        code, out = self.git("checkout", "-b", branch)
        if code != 0:
            return f"브랜치를 만들지 못했습니다: {out}"
        files = self.changed()
        self.git("add", "--", *files)
        code, out = self.git("commit", "-m", message)
        self.snapshot = {}
        return f"브랜치 {branch}에 커밋했습니다." if code == 0 else f"커밋 실패: {out}"
