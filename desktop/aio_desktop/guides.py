"""작성 가이드(취업 특강 정리본)를 문항 유형에 맞춰 골라 주고, 강의 예시 베끼기를 검사한다.

자료 폴더/guides/
  G1-….md, G2-….md          특강 정리본 (for_office의 guides/ 를 가져오거나 직접 넣음)
  sources/examples.txt      강의 예시 원문 (베끼기 검사 전용, AI에 보내지 않음)

원칙 (for_office CLAUDE.md와 같음)
- 가이드는 '쓰는 방법'의 근거일 뿐이다. 경험·수치·회사 정보의 근거가 아니다.
- 규칙과 가이드가 다르면 규칙이 우선 (소제목 필수, 다짐형 맺음 금지 등).
- 강의 예시 문장은 베끼지 않는다: 공백·문장부호를 뺀 20자 이상이 예시와 같으면 오류.
- 강의 슬라이드의 시장 수치는 회사 정보로 쓰지 않는다.
- 이 프로그램은 가이드를 내장하지 않는다 (강의 자료 저작권). 사용자의 자료 폴더에 있는 것만 읽는다.
"""
from __future__ import annotations

import re
from pathlib import Path

COPY_MIN = 20

# 문항 유형 판정 (제목 + 원문 키워드). 앞에서부터 먼저 맞는 것.
QTYPES = [
    ("motivation", "지원동기·입사 후 포부", r"지원\s*동기|지원한\s*(이유|동기)|입사\s*후|포부|커리어\s*(목표|계획)|왜\s*.*지원|기여\s*방안|꿈"),
    ("strengths", "강점·약점", r"강점|약점|장점|단점|성격"),
    ("industry", "시사·산업 이해", r"시사|산업\s*(동향|이해|방향)|트렌드|이슈|전망|사회\s*문제"),
    ("values", "인재상·가치", r"인재상|핵심\s*가치|가치관|가치를"),
    ("resume", "이력서 경력·프로젝트 칸", r"경력\s*사항|프로젝트\s*경험\s*(을|를)?\s*(기재|작성)|활동\s*내역|학내외"),
    ("experience", "경험 (직무역량·도전·성공/실패·협업)", r".*"),
]

# 유형별로 읽을 절. (파일 이름 앞부분, 제목에 들어 있는 말)  — for_office CLAUDE.md '작성 가이드' 표와 같음
SECTION_MAP = {
    "common": [("G2", "1. 대원칙"), ("G1", "5. 글쓰기 원칙"), ("G1", "7. FAQ"), ("G1", "8. 퇴고 체크")],
    "experience": [("G1", "1. 경험"), ("G1", "6. 항목별 구조"), ("G2", "### 직무역량")],
    "motivation": [("G2", "### 지원동기"), ("G2", "4. JD 읽는 법")],
    "strengths": [("G2", "### 강점·약점")],
    "industry": [("G2", "### 시사·산업")],
    "values": [("G2", "### 인재상")],
    "resume": [("G2", "3. 이력서")],
    "defense": [("G1", "2. 방산 어필"), ("G1", "3. 역량 한 조각"), ("G1", "4. 직무별 키워드")],
    "observed": [("G2", "5. 합격 자소서 관찰")],
}
QTYPE_NAME = {k: n for k, n, _ in QTYPES}


def qtype(q: dict) -> str:
    s = f"{q.get('title', '')} {q.get('text', '')}"
    for key, _, pat in QTYPES:
        if re.search(pat, s):
            return key
    return "experience"


class Guides:
    def __init__(self, root: Path):
        self.dir = Path(root) / "guides"
        self.files: dict[str, str] = {}
        self.sections: list[dict] = []      # {file, short, heading, text}
        if self.dir.is_dir():
            for p in sorted(self.dir.glob("*.md")):
                txt = p.read_text(encoding="utf-8", errors="replace")
                self.files[p.name] = txt
                self._split(p.name, txt)
        self.grams = self._load_examples(self.dir / "sources" / "examples.txt")

    # ---------- 절 나누기 ----------
    def _split(self, name: str, txt: str):
        short = re.match(r"(G\d+)", name).group(1) if re.match(r"G\d+", name) else Path(name).stem
        cur = None
        for line in txt.splitlines():
            m = re.match(r"^(#{2,3})\s+(.*)", line)
            if m:
                if cur:
                    self.sections.append(cur)
                cur = {"file": name, "short": short, "level": len(m.group(1)), "heading": m.group(2).strip(), "text": line + "\n"}
            elif cur:
                cur["text"] += line + "\n"
        if cur:
            self.sections.append(cur)
        # '## 2. 문항 유형별'처럼 바로 아래 ### 들만 있는 상위 절은 본문이 짧으니 그대로 둔다

    def ok(self) -> bool:
        return bool(self.sections)

    def _find(self, short: str, key: str) -> list[dict]:
        want_h3 = key.startswith("### ")
        k = key[4:] if want_h3 else key
        out = []
        for s in self.sections:
            if s["short"] != short:
                continue
            if want_h3 and s["level"] == 3 and s["heading"].startswith(k):
                out.append(s)
            elif not want_h3 and s["level"] == 2 and s["heading"].startswith(k):
                out.append(s)
                # 상위 절 밑의 ### (예: G1 6절의 '⚠ 예시를 그대로 따라 하면 안 되는 부분')도 같이
                i = self.sections.index(s) + 1
                while i < len(self.sections) and self.sections[i]["level"] == 3:
                    out.append(self.sections[i])
                    i += 1
        return out

    def label(self, s: dict) -> str:
        return f"{s['short']} {s['heading'].split('[')[0].strip()}"

    def plan(self, questions: list[dict], defense: bool) -> dict:
        """문항마다 유형과 읽을 절. 반환 {"per_q": {id: {"type", "type_name", "sections": [label]}}, "text": 프롬프트에 넣을 가이드 본문}"""
        if not self.ok():   # 가이드 파일이 없음
            return {"per_q": {q["id"]: {"type": qtype(q), "type_name": QTYPE_NAME[qtype(q)], "sections": []} for q in questions},
                    "text": "", "known": False}
        used: list[dict] = []

        def add(keys):
            got = []
            for short, key in keys:
                for s in self._find(short, key):
                    got.append(s)
                    if s not in used:
                        used.append(s)
            return got
        common = add(SECTION_MAP["common"])
        dsec = add(SECTION_MAP["defense"]) if defense else []
        add(SECTION_MAP["observed"])
        per = {}
        for q in questions:
            t = qtype(q)
            secs = add(SECTION_MAP[t])
            per[q["id"]] = {"type": t, "type_name": QTYPE_NAME[t], "sections": [self.label(s) for s in secs + dsec + common]}
        known = bool(used)
        text = "\n".join(f"〔{self.label(s)}〕\n{s['text'].strip()}\n" for s in used)
        # 사용자가 앱에서 넣은 가이드 (G1·G2 형식이 아닌 것): '문항별: 유형' 절은 그 유형 문항에만, 나머지 절은 모든 문항에
        mapped = {short for keys in SECTION_MAP.values() for short, _ in keys}
        own = [s for s in self.sections if s["short"] not in mapped or not known]
        if own:
            types = {v["type"] for v in per.values()}
            pats = {k: p for k, _, p in QTYPES if k != "experience"}
            picked = []
            for s in own:
                h = s["heading"]
                if h.startswith("문항별"):
                    hit = [t for t in types if t != "experience" and re.search(pats[t], h)]
                    exp_like = "experience" in types and not any(re.search(p, h) for p in pats.values())
                    if not hit and not exp_like:
                        continue
                    for qid, v in per.items():
                        if v["type"] in hit or (exp_like and v["type"] == "experience"):
                            v["sections"].append(self.label(s))
                else:
                    for v in per.values():
                        v["sections"].append(self.label(s))
                picked.append(s)
            # 같은 절 이름이 여러 번 붙지 않게
            for v in per.values():
                v["sections"] = list(dict.fromkeys(v["sections"]))
            text += "\n".join(f"〔{self.label(s)}〕\n{s['text'].strip()}\n" for s in picked)
        text = text[:30000]
        known = known or bool(own)
        return {"per_q": per, "text": text, "known": known}

    # ---------- 베끼기 검사 ----------
    @staticmethod
    def _norm(s: str) -> tuple[str, list[int]]:
        keep, pos = [], []
        for i, ch in enumerate(s):
            if ch.isalnum():
                keep.append(ch.lower())
                pos.append(i)
        return "".join(keep), pos

    def _load_examples(self, p: Path) -> dict[str, str]:
        if not p.exists():
            return {}
        grams, src = {}, "?"
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("## "):
                src = line[3:].strip()
                continue
            if line.startswith("#"):
                continue
            n, _ = self._norm(line)
            for i in range(len(n) - COPY_MIN + 1):
                grams.setdefault(n[i:i + COPY_MIN], src)
        return grams

    def copied(self, text: str) -> list[tuple[str, str]]:
        """강의 예시와 20자 이상 같은 구간 [(원문 조각, 출처 'G1 p.90')]."""
        if not self.grams:
            return []
        n, pos = self._norm(text)
        hit = [None] * len(n)
        for i in range(len(n) - COPY_MIN + 1):
            s = self.grams.get(n[i:i + COPY_MIN])
            if s:
                for j in range(i, i + COPY_MIN):
                    hit[j] = hit[j] or s
        out, i = [], 0
        while i < len(n):
            if hit[i]:
                j = i
                while j < len(n) and hit[j]:
                    j += 1
                out.append((text[pos[i]:pos[j - 1] + 1], hit[i]))
                i = j
            else:
                i += 1
        return out

    def summary(self) -> dict:
        return {"files": list(self.files), "sections": len(self.sections), "examples": bool(self.grams)}
