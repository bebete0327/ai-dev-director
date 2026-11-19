"""Validated, atomic per-provider CLI model preferences & live model catalog discovery (passed via --model; no API keys)."""
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.db_path import SOURCE_ROOT

PATH = SOURCE_ROOT / "data" / "model_settings.json"
LOCK = threading.Lock()
_DISCOVERY_LOCK = threading.Lock()
_DISCOVERY_CACHE: Dict[str, Any] = {"fetched_at": 0.0, "models": None}
_DISCOVERY_TTL_SEC = 120.0

PROVIDERS = ("claude", "antigravity", "openai")
ALIASES = {
    "codex": "openai",
    "openai": "openai",
    "claude": "claude",
    "anthropic": "claude",
    "google": "antigravity",
    "antigravity": "antigravity",
}
DOMAINS = {"openai": "OPENAI", "claude": "ANTHROPIC", "antigravity": "GOOGLE"}
DOMAIN_TO_PROVIDER = {"OPENAI": "openai", "ANTHROPIC": "claude", "GOOGLE": "antigravity"}

DEFAULT_SENTINELS = {
    "",
    "default",
    "cli-default",
    "claude-code-cli",
    "codex-cli",
    "antigravity-cli",
}

_SAFE_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()/\[\]:+-]{0,119}$")

PRESETS: Dict[str, List[Dict[str, str]]] = {
    "claude": [
        {"id": "", "label": "CLI 기본값", "hint": "내 Claude 계정 기본 모델"},
        {"id": "claude-fable-5-2", "label": "Claude Fable 5.2", "hint": "최신 5.2 심층 추론 · 코딩"},
        {"id": "fable", "label": "Claude Fable (최신)", "hint": "Fable 최신 별칭"},
        {"id": "claude-opus-5-5", "label": "Claude Opus 5.5", "hint": "최상위 코딩 · 아키텍처"},
        {"id": "opus", "label": "Opus (최신)", "hint": "심층 설계 · 복잡한 코딩"},
        {"id": "sonnet", "label": "Sonnet (최신)", "hint": "표준 빠른 개발 · 일상 작업"},
        {"id": "haiku", "label": "Haiku (최신)", "hint": "경량 고속 응답"},
    ],
    "antigravity": [
        {"id": "", "label": "CLI 기본값 (권장)", "hint": "내 요금제 기본 모델 자동 선택"},
        {"id": "gemini-4-hydrogen", "label": "Gemini 4 Hydrogen", "hint": "빠른 주력 구현 · 전 요금제 지원"},
        {"id": "gemini-4-neon", "label": "Gemini 4 Neon", "hint": "심층 추론 · 코딩 (Pro/Ultra)"},
        {"id": "gemini-4-argon", "label": "Gemini 4 Argon", "hint": "심층 설계 · 추론 (Pro/Ultra)"},
        {"id": "gemini-3.8-flash", "label": "Gemini 3.8 Flash", "hint": "초고속 경량 · 전 요금제 지원"},
        {"id": "gemini-4-carbon", "label": "Gemini 4 Carbon", "hint": "최상위 추론 (Ultra 전용)"},
        {"id": "gemini-pro-orchestrator-g1-pro", "label": "Orchestrator (Argon)", "hint": "계획+구현 협업 (Pro/Ultra)"},
        {"id": "Orchestrator", "label": "Orchestrator (Carbon)", "hint": "최상위 협업 에이전트 (Ultra 전용)"},
    ],
    "openai": [
        {"id": "", "label": "CLI 기본값", "hint": "내 Codex 계정 기본 모델"},
        {"id": "gpt-6.2-astra", "label": "GPT-6.2 Astra", "hint": "최신 6.2 최고 난이도 추론 · 검수"},
        {"id": "gpt-6-astra", "label": "GPT-6 Astra", "hint": "고난도 추론 · 정밀 검수"},
        {"id": "gpt-6.1-sol", "label": "GPT-6.1 Sol", "hint": "주력 코딩 에이전트 표준"},
        {"id": "gpt-6.1-terra", "label": "GPT-6.1 Terra", "hint": "빠른 검토 · 멀티모달"},
        {"id": "gpt-6-luna", "label": "GPT-6 Luna", "hint": "경량 고속 반복 작업"},
    ],
}

AGY_MODEL_ALIASES: Dict[str, str] = {
    "gemini 4 neon": "gemini-4-neon",
    "gemini 4 carbon": "gemini-4-carbon",
    "gemini 4 argon": "gemini-4-argon",
    "gemini 4 hydrogen": "gemini-4-hydrogen",
    "gemini 3.8 flash": "gemini-3.8-flash",
    "gemini 3.7 flash": "gemini-3.7-flash",
    "gemini 3.1 pro": "gemini-3.1-pro",
    "orchestrator": "Orchestrator",
    "orchestrator (carbon)": "Orchestrator",
    "orchestrator (neon)": "Orchestrator",
    "orchestrator (argon)": "gemini-pro-orchestrator-g1-pro",
}

EFFORT_PRESETS: Dict[str, List[Dict[str, str]]] = {
    "claude": [
        {"id": "", "label": "기본값", "hint": "CLI 기본 추론 강도"},
        {"id": "low", "label": "Low", "hint": "최소 사고 · 빠른 응답"},
        {"id": "medium", "label": "Medium", "hint": "표준 균형"},
        {"id": "high", "label": "High", "hint": "깊은 사고 · 탐색 강화"},
        {"id": "xhigh", "label": "XHigh", "hint": "매우 깊은 추론"},
        {"id": "max", "label": "Max", "hint": "최대 사고량 (무제한)"},
    ],
    "antigravity": [
        {"id": "", "label": "기본값", "hint": "CLI 기본 추론 강도"},
        {"id": "low", "label": "Low", "hint": "빠른 경량 사고"},
        {"id": "medium", "label": "Medium", "hint": "표준 균형"},
        {"id": "high", "label": "High", "hint": "깊은 설계 · 추론"},
        {"id": "xhigh", "label": "Extra (XHigh)", "hint": "최대 사고량 (Hydrogen/Neon)"},
    ],
    "openai": [
        {"id": "", "label": "기본값", "hint": "CLI 기본 추론 강도"},
        {"id": "minimal", "label": "Minimal", "hint": "최소 추론 · 초고속"},
        {"id": "low", "label": "Low", "hint": "가벼운 추론"},
        {"id": "medium", "label": "Medium", "hint": "표준 균형"},
        {"id": "high", "label": "High", "hint": "깊은 추론 · 정밀 검수"},
        {"id": "xhigh", "label": "XHigh", "hint": "최고 난이도 추론"},
    ],
}

_VALID_EFFORTS: Dict[str, set] = {
    prov: {item["id"] for item in items} for prov, items in EFFORT_PRESETS.items()
}


def normalize_provider(name: str) -> str:
    key = str(name or "").strip().lower()
    if key in ALIASES:
        return ALIASES[key]
    raise ValueError(f"알 수 없는 AI 공급자입니다: {name}")


def normalize_model(value: Optional[str]) -> str:
    """Return '' for CLI default, or a validated model identifier safe for shell=False CLI --model."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("모델 이름은 문자열이어야 합니다.")
    cleaned = value.strip()
    if cleaned.lower() in DEFAULT_SENTINELS:
        return ""
    if not _SAFE_MODEL_RE.fullmatch(cleaned):
        raise ValueError(f"유효하지 않은 모델 이름입니다: {cleaned[:60]}")
    return cleaned


def canonicalize_provider_model(provider: Optional[str], value: Optional[str]) -> str:
    """Normalize and map display labels (e.g. 'Gemini 4 Carbon') to exact CLI model slugs ('gemini-4-carbon')."""
    norm = normalize_model(value)
    if not norm:
        return ""
    prov = ALIASES.get(str(provider or "").strip().lower(), "")
    if prov == "antigravity":
        return AGY_MODEL_ALIASES.get(norm.lower(), norm)
    return norm


def is_effort_supported_for_model(provider: Optional[str], model_id: Optional[str]) -> bool:
    """Return False when a CLI rejects --effort for a specific model family (e.g. Antigravity Orchestrator or 3P)."""
    prov = ALIASES.get(str(provider or "").strip().lower(), "")
    mid = (model_id or "").strip().lower()
    if prov == "antigravity" and mid:
        if "orchestrator" in mid or mid.startswith(("claude", "gpt")):
            return False
    return True


def normalize_effort(provider: Optional[str], value: Optional[str]) -> str:
    """Return '' for CLI default, or a validated reasoning-effort token for the given provider."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("추론 강도는 문자열이어야 합니다.")
    cleaned = value.strip().lower()
    if cleaned in {"", "default", "auto", "cli-default"}:
        return ""
    if provider:
        prov = normalize_provider(provider)
        if prov == "antigravity" and cleaned == "max":
            cleaned = "xhigh"
        if cleaned not in _VALID_EFFORTS[prov]:
            raise ValueError(f"{prov}에서 지원하지 않는 추론 강도입니다: {cleaned}")
        return cleaned
    all_efforts = set().union(*_VALID_EFFORTS.values())
    if cleaned not in all_efforts:
        raise ValueError(f"지원하지 않는 추론 강도입니다: {cleaned}")
    return cleaned


def _read_raw() -> dict:
    if not PATH.exists():
        return {}
    try:
        raw = json.loads(PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _write_raw(payload: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, PATH)


def load() -> Dict[str, str]:
    base = {p: "" for p in PROVIDERS}
    raw = _read_raw()
    for k, v in raw.items():
        if str(k).strip().lower() in ALIASES:
            prov = ALIASES[str(k).strip().lower()]
            try:
                base[prov] = canonicalize_provider_model(prov, str(v) if v is not None else "")
            except ValueError:
                pass
    return base


def load_efforts() -> Dict[str, str]:
    base = {p: "" for p in PROVIDERS}
    raw = _read_raw()
    eff = raw.get("_efforts")
    if isinstance(eff, dict):
        for k, v in eff.items():
            key = str(k).strip().lower()
            if key in ALIASES:
                prov = ALIASES[key]
                try:
                    base[prov] = normalize_effort(prov, str(v) if v is not None else "")
                except ValueError:
                    pass
    return base


def _preserve_meta(raw: dict, out: dict) -> None:
    for meta_key in ("_efforts", "_custom_models", "_discovered_models"):
        if isinstance(raw.get(meta_key), dict):
            out[meta_key] = raw[meta_key]


def save(updates: dict) -> Dict[str, str]:
    if not isinstance(updates, dict) or not updates:
        raise ValueError("변경할 모델 설정을 지정해야 합니다.")
    parsed: Dict[str, str] = {}
    for k, v in updates.items():
        prov = normalize_provider(k)
        parsed[prov] = canonicalize_provider_model(prov, v)
    with LOCK:
        raw = _read_raw()
        merged = load()
        merged.update(parsed)
        out = dict(merged)
        _preserve_meta(raw, out)
        # Automatically remember any custom model ID entered by the user so it stays in the clickable menu
        custom = dict(out.get("_custom_models") or {})
        for prov, mid in parsed.items():
            if not mid:
                continue
            preset_ids = {item["id"].lower() for item in PRESETS.get(prov, [])}
            if mid.lower() not in preset_ids:
                lst = list(custom.get(prov) or [])
                if not any(isinstance(x, dict) and str(x.get("id", "")).lower() == mid.lower() for x in lst):
                    lst.insert(0, {"id": mid, "label": mid, "hint": "직접 추가한 모델"})
                    custom[prov] = lst[:15]
        if custom:
            out["_custom_models"] = custom
        _write_raw(out)
    return merged


def save_efforts(updates: dict) -> Dict[str, str]:
    if not isinstance(updates, dict) or not updates:
        raise ValueError("변경할 추론 강도를 지정해야 합니다.")
    parsed: Dict[str, str] = {}
    for k, v in updates.items():
        prov = normalize_provider(k)
        parsed[prov] = normalize_effort(prov, v)
    with LOCK:
        raw = _read_raw()
        merged_eff = load_efforts()
        merged_eff.update(parsed)
        out = load()
        _preserve_meta(raw, out)
        out["_efforts"] = merged_eff
        _write_raw(out)
    return merged_eff


def _pretty_model_label(model_id: str, display_name: str = "") -> str:
    if display_name and display_name.strip():
        return display_name.strip()
    s = model_id.strip()
    low = s.lower()
    if low.startswith("gemini-"):
        return s.replace("-", " ").title()
    if low.startswith("claude-"):
        parts = s.split("-")
        # e.g., claude-fable-5-2 -> Claude Fable 5.2
        if len(parts) >= 4 and parts[2].isdigit() and parts[3].isdigit():
            return f"Claude {parts[1].capitalize()} {parts[2]}.{parts[3]}"
        return s.replace("-", " ").title()
    if low.startswith("gpt-"):
        parts = s.split("-")
        if len(parts) >= 3:
            return f"GPT-{parts[1]} {' '.join(p.capitalize() for p in parts[2:])}"
        return s.upper()
    return s


def _discover_codex_models(codex_home: Optional[Path] = None, force: bool = False) -> List[Dict[str, str]]:
    home = codex_home or Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    found: List[Dict[str, str]] = []
    seen = set()

    def _add(mid: str, label: str = "", hint: str = "Codex 감지 모델") -> None:
        try:
            norm = normalize_model(mid)
        except ValueError:
            return
        if not norm or norm.lower() in seen:
            return
        seen.add(norm.lower())
        found.append({
            "id": norm,
            "label": _pretty_model_label(norm, label),
            "hint": (hint or "Codex 감지 모델")[:42],
        })

    # 1. ~/.codex/models_cache.json (official Codex CLI server model catalog cache)
    cache_file = home / "models_cache.json"
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            models_list = data.get("models") if isinstance(data, dict) else (data if isinstance(data, list) else [])
            if isinstance(models_list, list):
                for item in models_list:
                    if isinstance(item, dict):
                        mid = str(item.get("slug") or item.get("id") or item.get("model") or "").strip()
                        lbl = str(item.get("display_name") or item.get("name") or item.get("title") or "").strip()
                        desc = str(item.get("description") or "Codex CLI 지원 모델").strip()
                        if mid:
                            _add(mid, lbl, desc)
                    elif isinstance(item, str):
                        _add(item, "", "Codex CLI 지원 모델")
        except Exception:
            pass

    # 2. ~/.codex/config.toml
    cfg_toml = home / "config.toml"
    if cfg_toml.exists():
        try:
            for line in cfg_toml.read_text(encoding="utf-8", errors="ignore").splitlines():
                m = re.match(r'^\s*model\s*=\s*["\']([^"\']+)["\']', line)
                if m:
                    _add(m.group(1), "", "config.toml 설정 모델")
        except Exception:
            pass

    # 3. Recent ~/.codex/sessions/**/*.jsonl
    sess_dir = home / "sessions"
    if sess_dir.exists():
        try:
            files = sorted(sess_dir.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]
            for f in files:
                raw = f.read_text(encoding="utf-8", errors="ignore")[:65536]
                for m in re.finditer(r'"model"\s*:\s*"([^"]+)"', raw):
                    _add(m.group(1), "", "최근 Codex 세션 사용")
        except Exception:
            pass

    return found


def _discover_claude_models(claude_home: Optional[Path] = None, force: bool = False) -> List[Dict[str, str]]:
    home = claude_home or Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    found: List[Dict[str, str]] = []
    seen = set()

    def _add(mid: str, label: str = "", hint: str = "Claude 감지 모델") -> None:
        if not mid or mid.startswith("<"):
            return
        try:
            norm = normalize_model(mid)
        except ValueError:
            return
        if not norm or norm.lower() in seen:
            return
        seen.add(norm.lower())
        found.append({
            "id": norm,
            "label": _pretty_model_label(norm, label),
            "hint": (hint or "Claude 감지 모델")[:42],
        })

    # 1. Live Anthropic /v1/models endpoint when force-refreshing and OAuth credentials exist
    if force and os.environ.get("DIRECTOR_TESTING") != "1":
        cred_file = home / ".credentials.json"
        token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
        if not token and cred_file.exists():
            try:
                raw_c = json.loads(cred_file.read_text(encoding="utf-8"))
                oauth = (raw_c.get("claudeAiOauth") or raw_c.get("anthropic") or {}) if isinstance(raw_c, dict) else {}
                token = str(oauth.get("accessToken") or "").strip()
            except Exception:
                token = ""
        if token:
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/models",
                headers={
                    "Authorization": f"Bearer {token}",
                    "anthropic-version": "2023-06-01",
                    "anthropic-beta": "oauth-2025-04-20",
                    "User-Agent": "claude-code/2.1.289",
                    "Accept": "application/json",
                },
                method="GET",
            )
            try:
                with urllib.request.urlopen(req, timeout=2.5) as resp:
                    payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                if isinstance(payload, dict) and isinstance(payload.get("data"), list):
                    for item in payload["data"]:
                        if isinstance(item, dict):
                            mid = str(item.get("id") or "").strip()
                            dname = str(item.get("display_name") or "").strip()
                            if mid:
                                _add(mid, dname, "Anthropic 실시간 조회")
            except Exception:
                pass

    # 2. ~/.claude/settings.json
    settings_file = home / "settings.json"
    if settings_file.exists():
        try:
            st = json.loads(settings_file.read_text(encoding="utf-8"))
            if isinstance(st, dict) and isinstance(st.get("model"), str):
                _add(st["model"], "", "settings.json 설정 모델")
        except Exception:
            pass

    # 3. Recent ~/.claude/projects/**/*.jsonl
    proj_dir = home / "projects"
    if proj_dir.exists():
        try:
            files = sorted(proj_dir.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:15]
            for f in files:
                raw = f.read_text(encoding="utf-8", errors="ignore")
                for m in re.finditer(r'"model"\s*:\s*"([^"<]+)"', raw):
                    _add(m.group(1), "", "최근 Claude 세션 사용")
        except Exception:
            pass

    return found


def _discover_antigravity_models(vscdb_path: Optional[Path] = None, force: bool = False) -> List[Dict[str, str]]:
    found: List[Dict[str, str]] = []
    seen = set()

    def _add(mid: str, label: str = "", hint: str = "Antigravity 감지 모델") -> None:
        try:
            norm = canonicalize_provider_model("antigravity", mid)
        except ValueError:
            return
        if not norm or " " in norm or norm.lower() in seen:
            return
        seen.add(norm.lower())
        found.append({
            "id": norm,
            "label": _pretty_model_label(norm, label),
            "hint": (hint or "Antigravity 감지 모델")[:42],
        })

    # 1. Official 'agy models' CLI catalog probe on force refresh
    if force and os.environ.get("DIRECTOR_TESTING") != "1":
        try:
            from core.env_utils import resolve_cli, run_capture
            agy_bin = resolve_cli("agy")
            if agy_bin and Path(agy_bin).exists():
                res = run_capture([agy_bin, "models"], timeout=4)
                if res.returncode == 0 and res.stdout:
                    for m in re.finditer(r'\b((?:gemini|claude|gpt)-[a-z0-9._-]+|Orchestrator)\b', res.stdout):
                        _add(m.group(1), "", "agy models 실시간 조회")
        except Exception:
            pass

    from core import quota_tracker
    candidates = [vscdb_path] if vscdb_path is not None else quota_tracker._antigravity_vscdb_candidates()
    for p in candidates:
        if p is None or not p.exists():
            continue
        tmp_copy: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(prefix="agy_models_", suffix=".sqlite", delete=False) as tf:
                tmp_copy = Path(tf.name)
            shutil.copy2(p, tmp_copy)
            with sqlite3.connect(str(tmp_copy), timeout=2.0) as conn:
                rows = conn.execute(
                    "SELECT value FROM ItemTable WHERE key LIKE '%model%' OR key LIKE '%cascade%' OR key LIKE '%jetski%'"
                ).fetchall()
                for (val,) in rows:
                    s_val = val.decode("utf-8", errors="ignore") if isinstance(val, bytes) else str(val or "")
                    for m in re.finditer(r'"(?:model|slug|modelId)"\s*:\s*"((?:gemini|claude|gpt)-[a-z0-9._-]+|Orchestrator)"', s_val):
                        _add(m.group(1), "", "Antigravity 로컬 설정")
                    for m in re.finditer(r'"label"\s*:\s*"((?:Gemini|Orchestrator)[^"]{2,50})"', s_val):
                        raw_lbl = m.group(1).strip()
                        slug = AGY_MODEL_ALIASES.get(raw_lbl.lower(), "")
                        if slug:
                            _add(slug, raw_lbl, "Antigravity 로컬 설정")
        except Exception:
            pass
        finally:
            if tmp_copy is not None:
                try:
                    tmp_copy.unlink(missing_ok=True)
                except OSError:
                    pass
    return found


def discover_live_models(
    force: bool = False,
    codex_home: Optional[Path] = None,
    claude_home: Optional[Path] = None,
    agy_vscdb_path: Optional[Path] = None,
) -> Dict[str, List[Dict[str, str]]]:
    """Discover newly released or locally active models across Claude, Codex, and Antigravity."""
    now = time.monotonic()
    with _DISCOVERY_LOCK:
        if (
            not force
            and codex_home is None
            and claude_home is None
            and agy_vscdb_path is None
            and _DISCOVERY_CACHE["models"] is not None
            and (now - _DISCOVERY_CACHE["fetched_at"]) < _DISCOVERY_TTL_SEC
        ):
            return _DISCOVERY_CACHE["models"]

    discovered = {
        "claude": _discover_claude_models(claude_home, force=force),
        "openai": _discover_codex_models(codex_home, force=force),
        "antigravity": _discover_antigravity_models(agy_vscdb_path, force=force),
    }

    if codex_home is None and claude_home is None and agy_vscdb_path is None:
        with _DISCOVERY_LOCK:
            _DISCOVERY_CACHE["fetched_at"] = now
            _DISCOVERY_CACHE["models"] = discovered
        if force and any(discovered.values()):
            with LOCK:
                raw = _read_raw()
                raw["_discovered_models"] = discovered
                _write_raw(raw)

    return discovered


def apply(config: dict) -> dict:
    models = load()
    efforts = load_efforts()
    config["provider_models"] = dict(models)
    config["provider_efforts"] = dict(efforts)
    for role in ("head", "builder", "reviewer"):
        role_cfg = config.get(role)
        if not isinstance(role_cfg, dict):
            continue
        prov = ALIASES.get(str(role_cfg.get("provider", "")).strip().lower())
        if prov and models.get(prov):
            role_cfg["model"] = models[prov]
        if prov and efforts.get(prov):
            role_cfg["effort"] = efforts[prov]
    return config


def effective_model(provider: str, configured_model: Optional[str] = None, config: Optional[dict] = None) -> str:
    prov = ALIASES.get(str(provider or "").strip().lower(), "")
    explicit = canonicalize_provider_model(prov, configured_model)
    if explicit:
        return explicit
    if isinstance(config, dict) and isinstance(config.get("provider_models"), dict) and prov:
        from_cfg = canonicalize_provider_model(prov, config["provider_models"].get(prov))
        if from_cfg:
            return from_cfg
    if prov:
        return load().get(prov, "")
    return ""


def effective_effort(provider: str, configured_effort: Optional[str] = None, config: Optional[dict] = None) -> str:
    prov = ALIASES.get(str(provider or "").strip().lower(), "")
    explicit = normalize_effort(prov or None, configured_effort)
    if explicit:
        return explicit
    if isinstance(config, dict) and isinstance(config.get("provider_efforts"), dict) and prov:
        from_cfg = normalize_effort(prov, config["provider_efforts"].get(prov))
        if from_cfg:
            return from_cfg
    if prov:
        return load_efforts().get(prov, "")
    return ""


def domain_models(overrides: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    saved = load()
    out = {dom: saved.get(prov, "") for dom, prov in DOMAIN_TO_PROVIDER.items()}
    for k, v in (overrides or {}).items():
        dom = str(k or "").strip().upper()
        if dom in DOMAIN_TO_PROVIDER:
            prov = DOMAIN_TO_PROVIDER[dom]
            norm = canonicalize_provider_model(prov, v)
            if norm:
                out[dom] = norm
    return out


def domain_efforts(overrides: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    saved = load_efforts()
    out = {dom: saved.get(prov, "") for dom, prov in DOMAIN_TO_PROVIDER.items()}
    for k, v in (overrides or {}).items():
        dom = str(k or "").strip().upper()
        if dom in DOMAIN_TO_PROVIDER:
            norm = normalize_effort(DOMAIN_TO_PROVIDER[dom], v)
            if norm:
                out[dom] = norm
    return out


def catalog(
    refresh: bool = False,
    codex_home: Optional[Path] = None,
    claude_home: Optional[Path] = None,
    agy_vscdb_path: Optional[Path] = None,
) -> Dict[str, List[Dict[str, str]]]:
    """Return full per-provider model catalog combining presets, live CLI/API discovery, and user-saved models."""
    raw = _read_raw()
    saved_active = load()
    custom_map = raw.get("_custom_models") if isinstance(raw.get("_custom_models"), dict) else {}
    persisted_disc = raw.get("_discovered_models") if isinstance(raw.get("_discovered_models"), dict) else {}
    live_disc = discover_live_models(
        force=refresh,
        codex_home=codex_home,
        claude_home=claude_home,
        agy_vscdb_path=agy_vscdb_path,
    )

    out: Dict[str, List[Dict[str, str]]] = {}
    for prov, base_items in PRESETS.items():
        merged: List[Dict[str, str]] = []
        seen_ids = set()

        def _append_item(item: Dict[str, Any]) -> None:
            if not isinstance(item, dict):
                return
            raw_mid = str(item.get("id") if item.get("id") is not None else "")
            try:
                mid = canonicalize_provider_model(prov, raw_mid) if raw_mid else ""
            except ValueError:
                return
            if prov == "antigravity" and mid and " " in mid:
                return
            key = mid.lower()
            if key in seen_ids:
                return
            seen_ids.add(key)
            merged.append({
                "id": mid,
                "label": str(item.get("label") or mid or "CLI 기본값"),
                "hint": str(item.get("hint") or ""),
            })

        for b in base_items:
            _append_item(b)
        for d_item in (live_disc.get(prov) or []):
            _append_item(d_item)
        for p_item in (persisted_disc.get(prov) or []):
            _append_item(p_item)
        for c_item in (custom_map.get(prov) or []):
            _append_item(c_item)
        active_id = saved_active.get(prov, "")
        if active_id:
            _append_item({"id": active_id, "label": active_id, "hint": "현재 선택된 모델"})

        out[prov] = merged
    return out


def effort_catalog() -> Dict[str, List[Dict[str, str]]]:
    return {k: [dict(item) for item in items] for k, items in EFFORT_PRESETS.items()}
