"""보안 모드: 의존성 취약점 점검.

흐름: 잠금 파일에서 부품 목록(SBOM) 만들기 → OSV에서 알려진 취약점 찾기 → CISA KEV·FIRST EPSS 붙이기
     → 서비스 노출·데이터 등급(사용자 입력)·코드 안 사용 흔적 → 규칙으로 등급 → 상위 항목만 AI가 조치안 설명
     → 추천 버전이 패키지 저장소에 실제 있는지 확인 → 보고서. 실제 수정은 사람이 '수정 맡기기'를 눌러 코드 모드로 넘긴다.

등급은 AI가 아니라 규칙이 계산한다(같은 입력이면 같은 답). AI는 설명과 조치안 문장만 쓴다.
네트워크는 모두 키가 필요 없는 공개 API다: api.osv.dev, cisa.gov KEV 피드, api.first.org EPSS, pypi.org, registry.npmjs.org.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

from . import providers
from .workspace import Workspace

KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"


def http_json(url: str, body: dict | None = None, timeout: int = 30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET",
                                 headers={"content-type": "application/json", "user-agent": "AIOfficeDesktop"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------- 1. 부품 목록 ----------------
def _norm_py(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_components(ws: Workspace) -> list[dict]:
    """지원: requirements*.txt(== 고정), Pipfile.lock, poetry.lock, package-lock.json(v1~v3).
    반환: [{name, version, ecosystem, source}] (중복 제거)."""
    comps, seen = [], set()

    def add(name, ver, eco, src):
        name, ver = name.strip(), (ver or "").strip().lstrip("=v")
        if not name or not ver:
            return
        k = (eco, name.lower(), ver)
        if k not in seen:
            seen.add(k)
            comps.append({"name": name, "version": ver, "ecosystem": eco, "source": src})

    for rel in ws.files():
        base = rel.split("/")[-1]
        try:
            txt = ws.read(rel)
        except Exception:   # noqa: BLE001
            continue
        if re.fullmatch(r"requirements.*\.txt", base):
            for line in txt.splitlines():
                m = re.match(r"\s*([A-Za-z0-9_.\-]+)(\[[^\]]*\])?\s*==\s*([A-Za-z0-9_.\-+!]+)", line)
                if m:
                    add(m.group(1), m.group(3), "PyPI", rel)
        elif base == "Pipfile.lock":
            try:
                d = json.loads(txt)
            except json.JSONDecodeError:
                continue
            for sec in ("default", "develop"):
                for n, v in (d.get(sec) or {}).items():
                    add(n, (v or {}).get("version", ""), "PyPI", rel)
        elif base == "poetry.lock":
            for m in re.finditer(r'\[\[package\]\]\s*\nname\s*=\s*"([^"]+)"\s*\nversion\s*=\s*"([^"]+)"', txt):
                add(m.group(1), m.group(2), "PyPI", rel)
        elif base == "package-lock.json":
            try:
                d = json.loads(txt)
            except json.JSONDecodeError:
                continue
            for path, v in (d.get("packages") or {}).items():
                if path and "node_modules/" in path:
                    add(path.split("node_modules/")[-1], (v or {}).get("version", ""), "npm", rel)
            def walk(deps):
                for n, v in (deps or {}).items():
                    add(n, (v or {}).get("version", ""), "npm", rel)
                    walk((v or {}).get("dependencies"))
            if not d.get("packages"):
                walk(d.get("dependencies"))
    return comps


def to_cyclonedx(comps: list[dict], project: str) -> dict:
    def purl(c):
        eco = {"PyPI": "pypi", "npm": "npm"}[c["ecosystem"]]
        name = _norm_py(c["name"]) if eco == "pypi" else c["name"]
        return f"pkg:{eco}/{urllib.parse.quote(name, safe='@/')}@{c['version']}"
    return {"bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
            "metadata": {"timestamp": dt.datetime.now(dt.timezone.utc).isoformat(), "component": {"type": "application", "name": project},
                         "tools": [{"name": "AI Office Desktop"}]},
            "components": [{"type": "library", "name": c["name"], "version": c["version"], "purl": purl(c)} for c in comps]}


# ---------------- 2. 취약점 ----------------
def osv_lookup(comps: list[dict], fetch=http_json) -> list[dict]:
    """반환: [{component, id, aliases, summary, cvss, fixed}]"""
    out = []
    for i in range(0, len(comps), 500):
        chunk = comps[i:i + 500]
        q = {"queries": [{"package": {"name": c["name"], "ecosystem": c["ecosystem"]}, "version": c["version"]} for c in chunk]}
        res = fetch("https://api.osv.dev/v1/querybatch", q).get("results", [])
        for c, r in zip(chunk, res):
            for v in r.get("vulns") or []:
                d = fetch(f"https://api.osv.dev/v1/vulns/{v['id']}")
                out.append({"component": c, "id": d.get("id", v["id"]), "aliases": d.get("aliases", []),
                            "summary": d.get("summary") or (d.get("details") or "")[:200],
                            "cvss": _cvss(d), "fixed": _fixed(d, c), "refs": [x.get("url") for x in d.get("references", [])][:5]})
    return out


def _cvss(d: dict) -> float | None:
    """OSV의 severity 또는 database_specific에서 숫자 점수를 찾는다. 벡터만 있으면 None(등급표에 '미상')."""
    ds = d.get("database_specific") or {}
    for k in ("cvss_score", "score"):
        if isinstance(ds.get(k), (int, float)):
            return float(ds[k])
    sev = str(ds.get("severity", "")).upper()
    return {"CRITICAL": 9.5, "HIGH": 8.0, "MODERATE": 5.5, "MEDIUM": 5.5, "LOW": 2.5}.get(sev)


def _fixed(d: dict, c: dict) -> str:
    vers = []
    for a in d.get("affected", []):
        p = a.get("package", {})
        if p.get("ecosystem") != c["ecosystem"]:
            continue
        for rg in a.get("ranges", []):
            for e in rg.get("events", []):
                if "fixed" in e:
                    vers.append(e["fixed"])
    return sorted(set(vers), key=_vkey)[0] if vers else ""


def _vkey(v: str):
    return [(0, int(x), "") if x.isdigit() else (1, 0, x) for x in re.split(r"[.\-+]", v or "")]


def kev_set(fetch=http_json) -> dict:
    d = fetch(KEV_URL)
    return {x["cveID"]: x.get("dateAdded", "") for x in d.get("vulnerabilities", [])}


def epss_map(cves: list[str], fetch=http_json) -> dict:
    out = {}
    for i in range(0, len(cves), 100):
        q = ",".join(cves[i:i + 100])
        d = fetch("https://api.first.org/data/v1/epss?cve=" + urllib.parse.quote(q, safe=","))
        for x in d.get("data", []):
            out[x["cve"]] = float(x.get("epss", 0))
    return out


def version_exists(eco: str, name: str, ver: str, fetch=http_json) -> bool:
    try:
        if eco == "PyPI":
            fetch(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/{urllib.parse.quote(ver)}/json")
        else:
            fetch(f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='@')}/{urllib.parse.quote(ver)}")
        return True
    except Exception:   # noqa: BLE001
        return False


# ---------------- 3. 흔적 · 등급 ----------------
# 설치 이름과 import 이름이 다른 흔한 패키지
PY_IMPORT = {"pyyaml": "yaml", "pillow": "PIL", "beautifulsoup4": "bs4", "scikit-learn": "sklearn", "opencv-python": "cv2",
             "opencv-python-headless": "cv2", "python-dateutil": "dateutil", "pyjwt": "jwt", "protobuf": "google.protobuf",
             "python-dotenv": "dotenv", "pycryptodome": "Crypto", "msgpack-python": "msgpack", "pymysql": "pymysql"}

def usage_trace(ws: Workspace, comp: dict) -> list[str] | None:
    """코드에서 그 패키지를 import/require 하는 줄. 실제 호출 보장은 아니라 '흔적'으로만 쓴다.
    그 언어의 소스 파일이 하나도 없으면 None(미확인)."""
    n = comp["name"]
    mod = (PY_IMPORT.get(_norm_py(n)) or n.replace("-", "_").lower()) if comp["ecosystem"] == "PyPI" else n
    pats = ([re.compile(rf"^\s*(import|from)\s+{re.escape(mod)}\b", re.M | re.I)] if comp["ecosystem"] == "PyPI"
            else [re.compile(rf"(require\(|from\s+)['\"]{re.escape(mod)}(/|['\"])")])
    hits, scanned = [], 0
    exts = (".py",) if comp["ecosystem"] == "PyPI" else (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs")
    for rel in ws.files():
        if not rel.endswith(exts):
            continue
        scanned += 1
        try:
            txt = ws.read(rel)
        except Exception:   # noqa: BLE001
            continue
        for p in pats:
            m = p.search(txt)
            if m:
                line = txt[:m.start()].count("\n") + 1
                hits.append(f"{rel}:{line}")
                break
        if len(hits) >= 5:
            break
    return hits if scanned else None


def grade(cvss: float | None, kev: bool, epss: float | None, exposed: bool, data_level: int, trace: bool | None) -> tuple[str, int, dict]:
    """기획안의 규칙 그대로. 반환: (등급, 합계, 항목별 점수)."""
    s = {
        "심각도": 0 if cvss is None else (3 if cvss >= 9 else 2 if cvss >= 7 else 1 if cvss >= 4 else 0),
        "공격 확률": 0 if epss is None else (3 if epss >= 0.1 else 2 if epss >= 0.01 else 1 if epss >= 0.001 else 0),
        "외부 노출": 3 if exposed else 1,
        "데이터 등급": max(0, min(3, int(data_level))),
        # 직접 import가 없어도 다른 패키지를 거쳐 쓰일 수 있어 0이 아니라 1
        "사용 흔적": 3 if trace else (2 if trace is None else 1),
    }
    total = sum(s.values())
    if kev and exposed and trace:
        return "긴급", total, s
    return ("우선" if total >= 11 else "주시" if total >= 7 else "정기"), total, s


# ---------------- 4. 전체 실행 ----------------
ADVISOR = """너는 보안 담당자의 조치안 작성 보조다. 등급은 이미 규칙으로 정해졌으니 바꾸지 않는다.
주어진 취약점 정보만 근거로, 담당자에게 보낼 짧은 조치안을 쓴다. 정보에 없는 버전·기능을 지어내지 않는다.
답은 JSON 하나만: {{"items": [{{"key": "입력의 key 그대로", "why": "왜 이 등급인지 한두 문장", "action": "무엇을 어떤 버전으로 올릴지, 못 올리면 임시 조치", "ticket": "담당 팀에 보낼 티켓 제목"}}]}}
모든 글은 {lang}로."""


class SecurityRun:
    def __init__(self, cfg: dict, keys: dict, opts: dict, on_event, mock=None, fetch=http_json):
        self.cfg, self.keys, self.opts, self.on_event, self.mock, self.fetch = cfg, keys, opts, on_event, mock, fetch
        self.ws = Workspace(cfg["workspace"])
        self.state = "running"
        self.report: dict = {}
        self.stop_flag = __import__("threading").Event()

    def emit(self, role, text, kind="chat", **x):
        self.on_event({"time": dt.datetime.now().strftime("%H:%M:%S"), "role": role, "kind": kind, "text": text, **x})

    def run(self):
        try:
            comps = parse_components(self.ws)
            self.emit("scanner", f"잠금 파일에서 부품 {len(comps)}개를 찾았습니다." if comps else
                      "requirements.txt(== 고정), Pipfile.lock, poetry.lock, package-lock.json을 찾지 못했습니다.", "chat")
            if not comps:
                self.state = "done"; return
            bom = to_cyclonedx(comps, self.ws.root.name)
            vulns = osv_lookup(comps, self.fetch)
            self.emit("scanner", f"OSV에서 알려진 취약점 {len(vulns)}건을 찾았습니다.", "chat")
            cves = sorted({a for v in vulns for a in [v["id"], *v["aliases"]] if a.startswith("CVE-")})
            kev, epss = {}, {}
            try:
                kev = kev_set(self.fetch)
            except Exception as e:   # noqa: BLE001
                self.emit("system", f"KEV 목록을 받지 못해 KEV 없이 계산합니다: {e}", "info")
            try:
                epss = epss_map(cves, self.fetch) if cves else {}
            except Exception as e:   # noqa: BLE001
                self.emit("system", f"EPSS를 받지 못해 EPSS 없이 계산합니다: {e}", "info")
            exposed = bool(self.opts.get("exposed"))
            data_level = int(self.opts.get("data_level", 1))
            rows = []
            for v in vulns:
                ids = [v["id"], *v["aliases"]]
                cve = next((x for x in ids if x.startswith("CVE-")), "")
                tr = usage_trace(self.ws, v["component"])
                in_kev = cve in kev
                e = epss.get(cve)
                g, total, parts = grade(v["cvss"], in_kev, e, exposed, data_level, None if tr is None else bool(tr))
                rows.append({"key": f"{v['component']['name']}@{v['component']['version']}:{v['id']}",
                             "package": v["component"]["name"], "version": v["component"]["version"],
                             "ecosystem": v["component"]["ecosystem"], "source": v["component"]["source"],
                             "id": v["id"], "cve": cve, "summary": v["summary"], "cvss": v["cvss"],
                             "kev": in_kev, "kev_date": kev.get(cve, ""), "epss": e, "trace": tr,
                             "fixed": v["fixed"], "grade": g, "score": total, "parts": parts, "refs": v["refs"]})
            order = {"긴급": 0, "우선": 1, "주시": 2, "정기": 3}
            rows.sort(key=lambda r: (order[r["grade"]], -r["score"]))
            top = [r for r in rows if r["grade"] in ("긴급", "우선")][:8]
            for r in top:
                if r["fixed"]:
                    r["fixed_exists"] = version_exists(r["ecosystem"], r["package"], r["fixed"], self.fetch)
            counts = {k: sum(1 for r in rows if r["grade"] == k) for k in order}
            self.emit("judge", "규칙으로 등급을 매겼습니다: " + ", ".join(f"{k} {n}" for k, n in counts.items()), "chat")
            if top:
                rc = self.cfg["roles"]["advisor"]
                lang = self.cfg.get("lang", "한국어")
                payload = [{k: r[k] for k in ("key", "package", "version", "cve", "summary", "cvss", "kev", "epss", "trace", "fixed", "grade", "parts")} | {"fixed_exists": r.get("fixed_exists")} for r in top]
                text, _ = providers.chat(rc["provider"], rc.get("model", ""), self.keys.get(rc["provider"], ""),
                                         ADVISOR.format(lang=lang), json.dumps(payload, ensure_ascii=False), mock=self.mock)
                adv = {x.get("key"): x for x in providers.parse_json(text).get("items", [])}
                for r in top:
                    a = adv.get(r["key"], {})
                    r.update({"why": a.get("why", ""), "action": a.get("action", ""), "ticket": a.get("ticket", "")})
                    if r["fixed"] and r.get("fixed_exists") is False:
                        r["action"] = (r.get("action") or "") + f" ※ 추천 버전 {r['fixed']}을 패키지 저장소에서 찾지 못했습니다. 직접 확인하세요."
                self.emit("advisor", f"긴급·우선 {len(top)}건에 조치안을 붙였습니다.", "chat")
            self.report = {"project": self.ws.root.name, "time": dt.datetime.now().isoformat(timespec="seconds"),
                           "exposed": exposed, "data_level": data_level, "components": len(comps), "counts": counts, "rows": rows}
            out = self.ws.root / "ai-office-security"
            out.mkdir(exist_ok=True)
            (out / "sbom.cdx.json").write_text(json.dumps(bom, ensure_ascii=False, indent=2), encoding="utf-8")
            (out / "report.json").write_text(json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8")
            (out / "report.md").write_text(to_markdown(self.report), encoding="utf-8")
            self.emit("system", f"보고서와 SBOM을 {out}에 저장했습니다.", "report", report=self.report)
            self.state = "done"
        except Exception as e:   # noqa: BLE001
            self.state = "failed"
            self.emit("system", f"멈췄습니다: {type(e).__name__}: {e}", "error")


    def discard(self) -> str:
        self.state = "discarded"
        return "닫았습니다"

    def fix_task(self) -> str:
        """'수정 맡기기': 긴급·우선 중 고쳐진 버전이 확인된 것만 코드 모드 요청으로 바꾼다."""
        rows = [r for r in self.report.get("rows", []) if r["grade"] in ("긴급", "우선") and r["fixed"] and r.get("fixed_exists")]
        if not rows:
            return ""
        seen, lines = set(), []
        for r in rows:
            k = (r["package"], r["source"])
            if k in seen:
                continue
            seen.add(k)
            best = max((x["fixed"] for x in rows if (x["package"], x["source"]) == k), key=_vkey)
            lines.append(f"- {r['source']}: {r['package']} {r['version']} → {best} ({r['grade']}, {r['cve'] or r['id']})")
        return ("보안 점검 결과로 아래 의존성 버전만 올려 줘. 다른 코드는 바꾸지 말고, 잠금 파일은 사람이 다시 만들 수 있게 "
                "어떤 명령으로 갱신해야 하는지 note에 적어 줘. 호환성 문제가 보이면 고치지 말고 리뷰에 적어.\n" + "\n".join(lines))


def to_markdown(rep: dict) -> str:
    L = [f"# 의존성 취약점 점검 · {rep['project']}", "",
         f"- 시각: {rep['time']}  · 부품 {rep['components']}개  · 외부 노출: {'예' if rep['exposed'] else '아니오'}  · 데이터 등급: {rep['data_level']}",
         "- 등급: " + ", ".join(f"{k} {v}" for k, v in rep["counts"].items()),
         "- 등급은 규칙으로 계산했고, 사용 흔적은 import 검색 결과라 실제 호출을 보장하지 않습니다.", "",
         "| 등급 | 점수 | 패키지 | 취약점 | KEV | EPSS | 사용 흔적 | 고쳐진 버전 |", "|--|--|--|--|--|--|--|--|"]
    for r in rep["rows"]:
        epss = "" if r["epss"] is None else "%.3f" % r["epss"]
        kev = "예" if r["kev"] else ""
        trace = "미확인" if r["trace"] is None else (", ".join(r["trace"][:2]) or "직접 import 없음")
        L.append(f"| {r['grade']} | {r['score']} | {r['package']} {r['version']} | {r['cve'] or r['id']} | "
                 f"{kev} | {epss} | {trace} | {r['fixed']} |")
    for r in rep["rows"]:
        if r.get("action"):
            L += ["", f"## {r['grade']} · {r['package']} {r['version']} · {r['cve'] or r['id']}",
                  f"- 이유: {r.get('why', '')}", f"- 조치: {r['action']}", f"- 티켓: {r.get('ticket', '')}"]
    return "\n".join(L) + "\n"
