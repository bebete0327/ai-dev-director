"""Validated, atomic per-provider CLI model preferences (passed via official --model flag; no API keys)."""
import json
import os
import re
import threading
from typing import Dict, List, Optional

from core.db_path import SOURCE_ROOT

PATH = SOURCE_ROOT / "data" / "model_settings.json"
LOCK = threading.Lock()

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
        {"id": "opus", "label": "Opus (최신)", "hint": "심층 설계 · 복잡한 코딩"},
        {"id": "claude-opus-5-5", "label": "Claude Opus 5.5", "hint": "최상위 코딩 · 아키텍처"},
        {"id": "sonnet", "label": "Sonnet (최신)", "hint": "표준 빠른 개발 · 일상 작업"},
        {"id": "fable", "label": "Claude Fable", "hint": "장시간 심층 추론"},
        {"id": "haiku", "label": "Haiku (최신)", "hint": "경량 고속 응답"},
    ],
    "antigravity": [
        {"id": "", "label": "CLI 기본값", "hint": "내 Antigravity 계정 기본 모델"},
        {"id": "Orchestrator (Argon)", "label": "Orchestrator (Argon)", "hint": "계획 Argon + 구현 Hydrogen 협업"},
        {"id": "Orchestrator (Carbon)", "label": "Orchestrator (Carbon)", "hint": "계획 Carbon + 구현 Hydrogen"},
        {"id": "Gemini 4 Hydrogen", "label": "Gemini 4 Hydrogen", "hint": "빠른 주력 구현 에이전트"},
        {"id": "Gemini 4 Argon", "label": "Gemini 4 Argon", "hint": "심층 추론 · 설계"},
        {"id": "Gemini 4 Carbon", "label": "Gemini 4 Carbon", "hint": "최상위 추론"},
        {"id": "Gemini 3.8 Flash", "label": "Gemini 3.8 Flash", "hint": "초고속 경량"},
    ],
    "openai": [
        {"id": "", "label": "CLI 기본값", "hint": "내 Codex 계정 기본 모델"},
        {"id": "gpt-6-astra", "label": "GPT-6 Astra", "hint": "최고 난이도 추론 · 정밀 검수"},
        {"id": "gpt-6.1-sol", "label": "GPT-6.1 Sol", "hint": "주력 코딩 에이전트 표준"},
        {"id": "gpt-6.1-terra", "label": "GPT-6.1 Terra", "hint": "빠른 검토 · 멀티모달"},
        {"id": "gpt-6-luna", "label": "GPT-6 Luna", "hint": "경량 고속 반복 작업"},
    ],
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
        {"id": "max", "label": "Max", "hint": "최대 사고량"},
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
            try:
                base[ALIASES[str(k).strip().lower()]] = normalize_model(str(v) if v is not None else "")
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


def save(updates: dict) -> Dict[str, str]:
    if not isinstance(updates, dict) or not updates:
        raise ValueError("변경할 모델 설정을 지정해야 합니다.")
    parsed: Dict[str, str] = {}
    for k, v in updates.items():
        prov = normalize_provider(k)
        parsed[prov] = normalize_model(v)
    with LOCK:
        raw = _read_raw()
        merged = load()
        merged.update(parsed)
        out = dict(merged)
        if isinstance(raw.get("_efforts"), dict):
            out["_efforts"] = raw["_efforts"]
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
        out["_efforts"] = merged_eff
        _write_raw(out)
    return merged_eff


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
    explicit = normalize_model(configured_model)
    if explicit:
        return explicit
    if isinstance(config, dict) and isinstance(config.get("provider_models"), dict) and prov:
        from_cfg = normalize_model(config["provider_models"].get(prov))
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
            norm = normalize_model(v)
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


def catalog() -> Dict[str, List[Dict[str, str]]]:
    return {k: [dict(item) for item in items] for k, items in PRESETS.items()}


def effort_catalog() -> Dict[str, List[Dict[str, str]]]:
    return {k: [dict(item) for item in items] for k, items in EFFORT_PRESETS.items()}

