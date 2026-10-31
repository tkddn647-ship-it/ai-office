"""설정 저장. 일반 설정은 %APPDATA%\\AIOfficeDesktop\\config.json,
API 키는 Windows 자격 증명 관리자(keyring)에 저장한다. keyring을 못 쓰면 사용자 폴더 파일에 저장하고 화면에 알린다."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .providers import PROVIDERS

APP = "AIOfficeDesktop"


def app_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    d = Path(base) / APP
    d.mkdir(parents=True, exist_ok=True)
    return d


DEFAULT = {
    "workspace": "",
    "test_command": "",
    "lang": "한국어",
    "max_tasks": 3,
    "max_fix_rounds": 2,
    "max_calls": 12,
    "roles": {
        "planner": {"provider": "anthropic", "model": PROVIDERS["anthropic"]["default_model"]},
        "builder": {"provider": "openai", "model": PROVIDERS["openai"]["default_model"]},
        "reviewer": {"provider": "gemini", "model": PROVIDERS["gemini"]["default_model"]},
        "advisor": {"provider": "openai", "model": PROVIDERS["openai"]["default_model"]},
        "writer": {"provider": "anthropic", "model": PROVIDERS["anthropic"]["default_model"]},
        "judge": {"provider": "openai", "model": PROVIDERS["openai"]["default_model"]},
    },
    # 화면 입력을 다음 실행에도 남겨 둔다 (포트가 매번 바뀌어 브라우저 저장소를 못 쓰므로)
    "security": {"exposed": True, "data_level": 1},
    "jaso": {"exp_dir": "", "company": "", "role": "", "jd": "", "company_info": "", "questions": [],
             "target_ai": 2, "target_avg": 3.5, "max_rounds": 3},
}


def load_config() -> dict:
    p = app_dir() / "config.json"
    cfg = json.loads(json.dumps(DEFAULT))
    if p.exists():
        try:
            saved = json.loads(p.read_text(encoding="utf-8"))
            cfg.update({k: v for k, v in saved.items() if k != "roles"})
            for r, v in (saved.get("roles") or {}).items():
                if r in cfg["roles"] and isinstance(v, dict):
                    cfg["roles"][r].update(v)
        except (OSError, json.JSONDecodeError):
            pass
    return cfg


def save_config(cfg: dict):
    clean = {k: v for k, v in cfg.items() if k in DEFAULT}
    (app_dir() / "config.json").write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- 키 ----------
try:
    import keyring   # Windows: 자격 증명 관리자
    _HAS_KEYRING = True
except Exception:   # noqa: BLE001
    keyring = None
    _HAS_KEYRING = False


def key_store() -> str:
    return "Windows 자격 증명 관리자" if _HAS_KEYRING else f"파일 ({app_dir() / 'keys.json'})"


def _file_keys() -> dict:
    p = app_dir() / "keys.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def get_keys() -> dict:
    out = {}
    for prov in PROVIDERS:
        if prov == "mock":
            continue
        v = None
        if _HAS_KEYRING:
            try:
                v = keyring.get_password(APP, prov)
            except Exception:   # noqa: BLE001
                v = None
        if not v:
            v = _file_keys().get(prov)
        if v:
            out[prov] = v
    return out


def set_key(prov: str, value: str):
    if prov not in PROVIDERS or prov == "mock":
        return
    if _HAS_KEYRING:
        try:
            if value:
                keyring.set_password(APP, prov, value)
            else:
                try:
                    keyring.delete_password(APP, prov)
                except Exception:   # noqa: BLE001
                    pass
            return
        except Exception:   # noqa: BLE001
            pass
    d = _file_keys()
    if value:
        d[prov] = value
    else:
        d.pop(prov, None)
    p = app_dir() / "keys.json"
    p.write_text(json.dumps(d), encoding="utf-8")
