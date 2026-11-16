"""Per-model & per-provider usage + live CLI rate-limit/quota tracker for the UI."""
import base64
import json
import os
import re
import shutil
import sqlite3
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core import model_settings
from core.db_path import get_db_path

_CLAUDE_API_LOCK = threading.Lock()
_CLAUDE_API_CACHE: Dict[str, Any] = {"fetched_at": 0.0, "data": None}
_CODEX_API_LOCK = threading.Lock()
_CODEX_API_CACHE: Dict[str, Any] = {"fetched_at": 0.0, "data": None}
_AGY_API_LOCK = threading.Lock()
_AGY_API_CACHE: Dict[str, Any] = {"fetched_at": 0.0, "data": None}
_API_TTL_SEC = 180.0

_WEEKDAYS_KO = ("월", "화", "수", "목", "금", "토", "일")
_YA29_RE = re.compile(r"(ya29\.[A-Za-z0-9_\-\.]{25,})")
_B64_CHUNK_RE = re.compile(r"[A-Za-z0-9+/=_=-]{32,}")
_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_AGY_USER_AGENT = (
    "antigravity/hub/2.25.0 "
    "(aidev_client; os_type=windows; arch=amd64; cl=1010700967; auth_method=consumer)"
)


def _codex_root() -> Path:
    base = os.environ.get("CODEX_HOME")
    if base:
        return Path(base) / "sessions"
    return Path.home() / ".codex" / "sessions"


def _codex_auth_path() -> Path:
    base = os.environ.get("CODEX_HOME")
    if base:
        return Path(base) / "auth.json"
    return Path.home() / ".codex" / "auth.json"


def _claude_root() -> Path:
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg:
        return Path(cfg) / "projects"
    return Path.home() / ".claude" / "projects"


def _claude_credentials_path() -> Path:
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg:
        return Path(cfg) / ".credentials.json"
    return Path.home() / ".claude" / ".credentials.json"


def _antigravity_vscdb_candidates() -> List[Path]:
    candidates: List[Path] = []
    env_path = os.environ.get("AGY_STATE_VSCDB", "").strip()
    if env_path:
        candidates.append(Path(env_path))
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        candidates.append(Path(appdata) / "Antigravity" / "User" / "globalStorage" / "state.vscdb")
        candidates.append(Path(appdata) / "Antigravity IDE" / "User" / "globalStorage" / "state.vscdb")
    home = Path.home()
    candidates.extend([
        home / "AppData" / "Roaming" / "Antigravity" / "User" / "globalStorage" / "state.vscdb",
        home / "AppData" / "Roaming" / "Antigravity IDE" / "User" / "globalStorage" / "state.vscdb",
        home / ".config" / "Antigravity" / "User" / "globalStorage" / "state.vscdb",
        home / "Library" / "Application Support" / "Antigravity" / "User" / "globalStorage" / "state.vscdb",
    ])
    seen = set()
    unique: List[Path] = []
    for p in candidates:
        s = str(p)
        if s not in seen:
            seen.add(s)
            unique.append(p)
    return unique


def _num_or_none(v: Any) -> Optional[float]:
    try:
        return round(float(v), 1) if v is not None else None
    except (TypeError, ValueError):
        return None


def _fmt_pct(val: Optional[float]) -> str:
    if val is None:
        return "0%"
    v = round(float(val), 1)
    if v.is_integer():
        return f"{int(v)}%"
    return f"{v:.1f}%"


def _win_state(used_pct: Optional[float]) -> str:
    if used_pct is None:
        return "ok"
    if used_pct >= 95.0:
        return "exhausted"
    if used_pct >= 75.0:
        return "warn"
    return "ok"


def _parse_epoch_seconds(val: Any) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        v = float(val)
        return v / 1000.0 if v > 1e12 else v
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        try:
            v = float(s)
            return v / 1000.0 if v > 1e12 else v
        except ValueError:
            pass
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    return None


def _format_reset_ko(
    resets_at_raw: Any,
    reset_after_seconds: Any = None,
    style: str = "claude",
    now_ts: Optional[float] = None,
) -> str:
    """Format a reset timestamp/countdown into Korean UI strings matching each provider's native display."""
    now = now_ts if now_ts is not None else time.time()
    reset_ts = _parse_epoch_seconds(resets_at_raw)
    if reset_ts is None and reset_after_seconds is not None:
        try:
            reset_ts = now + float(reset_after_seconds)
        except (TypeError, ValueError):
            reset_ts = None
    if reset_ts is None:
        return ""
    diff = reset_ts - now
    if diff <= 0:
        return "초기화됨"

    dt = datetime.fromtimestamp(reset_ts)
    wd = _WEEKDAYS_KO[dt.weekday()]
    ampm = "오전" if dt.hour < 12 else "오후"
    h12 = dt.hour % 12 or 12
    clock_str = f"{ampm} {h12}:{dt.minute:02d}"

    days = int(diff // 86400)
    hours = int((diff % 86400) // 3600)
    mins = int((diff % 3600) // 60)
    if days > 0:
        rel = f"{days}일 {hours}시간 후 초기화" if hours > 0 else f"{days}일 후 초기화"
    elif hours > 0:
        rel = f"{hours}시간 {mins}분 후 초기화" if mins > 0 else f"{hours}시간 후 초기화"
    else:
        rel = f"{max(1, mins)}분 후 초기화"

    if style == "claude":
        return f"({wd}) {clock_str}에 초기화"
    if style == "codex":
        return f"{rel} ({dt.month}/{dt.day} {clock_str})"
    return rel


def _parse_agy_description_reset_ko(
    description: Any,
    reset_time_raw: Any = None,
    now_ts: Optional[float] = None,
) -> str:
    desc = str(description or "").strip()
    low = desc.lower()
    if "not used any" in low:
        return "미사용"
    m_refresh = re.search(r"refresh\s+in\s+([^.]+)", low)
    if m_refresh:
        span = m_refresh.group(1)
        m_d = re.search(r"(\d+)\s*days?", span)
        m_h = re.search(r"(\d+)\s*hours?", span)
        m_m = re.search(r"(\d+)\s*(?:minutes?|mins?)", span)
        parts: List[str] = []
        if m_d:
            parts.append(f"{int(m_d.group(1))}일")
        if m_h:
            parts.append(f"{int(m_h.group(1))}시간")
        if m_m and not m_d:
            parts.append(f"{int(m_m.group(1))}분")
        if parts:
            return " ".join(parts) + " 후 초기화"
    if reset_time_raw is not None:
        return _format_reset_ko(reset_time_raw, style="agy", now_ts=now_ts)
    return ""


def _window_pct(used_raw: Any, resets_at_raw: Any, now_ts: Optional[float] = None) -> Dict[str, Any]:
    """Return normalized percent & expired status. If resets_at is in the past, percent is 0.0 (window reset)."""
    now = now_ts if now_ts is not None else time.time()
    pct = _num_or_none(used_raw)
    reset_ts = _parse_epoch_seconds(resets_at_raw)
    if reset_ts is not None and reset_ts <= now:
        return {"percent": 0.0, "resets_at": resets_at_raw, "expired": True}
    return {"percent": pct, "resets_at": resets_at_raw, "expired": False}


def _build_codex_unified_window(
    used_pct: Optional[float],
    resets_at: Any,
    reset_after_seconds: Any = None,
    expired: bool = False,
    now_ts: Optional[float] = None,
) -> List[Dict[str, Any]]:
    if used_pct is None:
        return []
    u = max(0.0, min(100.0, float(used_pct)))
    rem = round(max(0.0, 100.0 - u), 1)
    reset_text = "초기화됨" if expired else _format_reset_ko(
        resets_at,
        reset_after_seconds=reset_after_seconds,
        style="codex",
        now_ts=now_ts,
    )
    right_text = f"{reset_text} · {_fmt_pct(rem)} 남음" if reset_text else f"{_fmt_pct(rem)} 남음 ({_fmt_pct(u)} 사용)"
    return [{
        "id": "codex_weekly",
        "label": "주간 사용 한도",
        "used_pct": round(u, 1),
        "remaining_pct": rem,
        "bar_pct": round(u, 1),
        "reset_text": reset_text,
        "right_text": right_text,
        "resets_at": resets_at,
        "expired": expired,
        "state": _win_state(u),
    }]


def _read_codex_auth_tokens(auth_path: Optional[Path] = None) -> Dict[str, str]:
    path = auth_path or _codex_auth_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        tokens = raw.get("tokens") if isinstance(raw.get("tokens"), dict) else raw
        return {
            "access_token": str(tokens.get("access_token") or "").strip(),
            "account_id": str(tokens.get("account_id") or "").strip(),
            "refresh_token": str(tokens.get("refresh_token") or "").strip(),
        }
    except Exception:
        return {}


def _refresh_codex_access_token(refresh_token: str) -> Optional[str]:
    if not refresh_token:
        return None
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": _CODEX_CLIENT_ID,
        "refresh_token": refresh_token,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://auth.openai.com/oauth/token",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            if isinstance(data, dict):
                tok = str(data.get("access_token") or "").strip()
                return tok or None
    except Exception:
        pass
    return None


def _parse_codex_wham_payload(payload: Dict[str, Any], now_ts: Optional[float] = None) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    rl = payload.get("rate_limit")
    if not isinstance(rl, dict):
        return None
    prim = rl.get("primary_window") if isinstance(rl.get("primary_window"), dict) else {}
    sec = rl.get("secondary_window") if isinstance(rl.get("secondary_window"), dict) else {}
    limit_reached = bool(rl.get("limit_reached")) or (rl.get("allowed") is False)

    w_prim = _window_pct(prim.get("used_percent"), prim.get("reset_at") or prim.get("resets_at"), now_ts=now_ts)
    w_sec = _window_pct(sec.get("used_percent"), sec.get("reset_at") or sec.get("resets_at"), now_ts=now_ts)

    # Choose the unified weekly window (secondary if present, otherwise primary)
    if sec and w_sec["percent"] is not None:
        target_win = sec
        target_calc = w_sec
    else:
        target_win = prim
        target_calc = w_prim

    used_pct = target_calc["percent"]
    if limit_reached and not target_calc["expired"]:
        used_pct = 100.0
    elif w_prim["percent"] is not None and w_prim["percent"] >= 100.0 and not w_prim["expired"]:
        used_pct = 100.0

    resets_at = target_win.get("reset_at") or target_win.get("resets_at")
    reset_after = target_win.get("reset_after_seconds")
    windows = _build_codex_unified_window(
        used_pct=used_pct,
        resets_at=resets_at,
        reset_after_seconds=reset_after,
        expired=target_calc["expired"],
        now_ts=now_ts,
    )
    if not windows:
        return None

    return {
        "source": "codex-wham",
        "primary_percent": used_pct,
        "primary_resets_at": resets_at,
        "primary_expired": target_calc["expired"],
        "secondary_percent": w_sec["percent"],
        "secondary_resets_at": sec.get("reset_at") or sec.get("resets_at"),
        "secondary_expired": w_sec["expired"],
        "windows": windows,
    }


def _fetch_codex_wham_usage(
    force: bool = False,
    auth_path: Optional[Path] = None,
    now_ts: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Query ChatGPT/Codex backend /wham/usage endpoint for live unified weekly rate limit."""
    if os.environ.get("DIRECTOR_TESTING") == "1" and auth_path is None:
        return None
    now = time.monotonic()
    with _CODEX_API_LOCK:
        if not force and _CODEX_API_CACHE["data"] is not None and (now - _CODEX_API_CACHE["fetched_at"]) < _API_TTL_SEC:
            return _CODEX_API_CACHE["data"]

    tokens = _read_codex_auth_tokens(auth_path)
    access_token = tokens.get("access_token", "")
    if not access_token:
        return None

    def _call(tok: str) -> Dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {tok}",
            "User-Agent": "codex-cli/0.116.0",
            "Accept": "application/json",
        }
        if tokens.get("account_id"):
            headers["ChatGPT-Account-Id"] = tokens["account_id"]
        req = urllib.request.Request(
            "https://chatgpt.com/backend-api/wham/usage",
            headers=headers,
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    payload = None
    try:
        payload = _call(access_token)
    except urllib.error.HTTPError as e:
        if e.code == 401 and tokens.get("refresh_token"):
            new_tok = _refresh_codex_access_token(tokens["refresh_token"])
            if new_tok:
                try:
                    payload = _call(new_tok)
                except Exception:
                    payload = None
    except Exception:
        with _CODEX_API_LOCK:
            return _CODEX_API_CACHE["data"]

    parsed = _parse_codex_wham_payload(payload, now_ts=now_ts) if isinstance(payload, dict) else None
    if parsed:
        with _CODEX_API_LOCK:
            _CODEX_API_CACHE["fetched_at"] = now
            _CODEX_API_CACHE["data"] = parsed
    return parsed


def _read_codex_rate_limits(root: Optional[Path] = None, now_ts: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Read the newest rate_limits block from ~/.codex/sessions/**/rollout-*.jsonl and check resets_at expiration."""
    base = root or _codex_root()
    if not base.exists():
        return None
    try:
        files = sorted(base.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:6]
    except OSError:
        return None
    for path in files:
        try:
            size = path.stat().st_size
            with path.open("rb") as fh:
                if size > 131072:
                    fh.seek(size - 131072)
                raw = fh.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        for line in reversed(raw.splitlines()):
            if "rate_limits" not in line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            rl = None
            if isinstance(obj, dict):
                if isinstance(obj.get("rate_limits"), dict):
                    rl = obj["rate_limits"]
                elif isinstance(obj.get("payload"), dict):
                    p = obj["payload"]
                    if isinstance(p.get("rate_limits"), dict):
                        rl = p["rate_limits"]
                    elif isinstance(p.get("info"), dict) and isinstance(p["info"].get("rate_limits"), dict):
                        rl = p["info"]["rate_limits"]
            if isinstance(rl, dict) and (isinstance(rl.get("primary"), dict) or isinstance(rl.get("secondary"), dict)):
                prim = rl.get("primary") or {}
                sec = rl.get("secondary") or {}
                w_prim = _window_pct(prim.get("used_percent"), prim.get("resets_at"), now_ts=now_ts)
                w_sec = _window_pct(sec.get("used_percent"), sec.get("resets_at"), now_ts=now_ts)

                # Determine unified weekly window for UI display
                sec_mins = int(sec.get("window_minutes") or 0) if isinstance(sec, dict) else 0
                if sec and w_sec["percent"] is not None and not w_sec["expired"] and (sec_mins >= 1440 or sec_mins == 0):
                    u_pct = w_sec["percent"]
                    if w_prim["percent"] is not None and w_prim["percent"] >= 100.0 and not w_prim["expired"]:
                        u_pct = 100.0
                    u_resets = sec.get("resets_at")
                    u_exp = w_sec["expired"]
                else:
                    u_pct = w_prim["percent"]
                    u_resets = prim.get("resets_at")
                    u_exp = w_prim["expired"]

                windows = _build_codex_unified_window(
                    used_pct=u_pct,
                    resets_at=u_resets,
                    expired=u_exp,
                    now_ts=now_ts,
                )
                return {
                    "source": "codex-session",
                    "primary_percent": w_prim["percent"],
                    "primary_resets_at": prim.get("resets_at"),
                    "primary_expired": w_prim["expired"],
                    "secondary_percent": w_sec["percent"],
                    "secondary_resets_at": sec.get("resets_at"),
                    "secondary_expired": w_sec["expired"],
                    "windows": windows,
                }
    return None


def _read_claude_oauth_token(cred_path: Optional[Path] = None) -> Optional[str]:
    env_tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if env_tok:
        return env_tok
    path = cred_path or _claude_credentials_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            oauth = raw.get("claudeAiOauth") or raw.get("anthropic") or {}
            if isinstance(oauth, dict):
                tok = str(oauth.get("accessToken") or "").strip()
                if tok:
                    return tok
    except Exception:
        pass
    return None


def _make_claude_window(
    win_id: str,
    label: str,
    win_dict: Dict[str, Any],
    now_ts: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    if not isinstance(win_dict, dict):
        return None
    raw_pct = (
        win_dict.get("utilization")
        if "utilization" in win_dict
        else (win_dict.get("used_percentage") if "used_percentage" in win_dict else win_dict.get("percent"))
    )
    if raw_pct is None and "used_percent" in win_dict:
        raw_pct = win_dict.get("used_percent")
    resets_at = win_dict.get("resets_at") or win_dict.get("reset_at")
    w = _window_pct(raw_pct, resets_at, now_ts=now_ts)
    if w["percent"] is None:
        return None
    used_pct = max(0.0, min(100.0, float(w["percent"])))
    rem_pct = round(max(0.0, 100.0 - used_pct), 1)
    reset_text = "초기화됨" if w["expired"] else _format_reset_ko(resets_at, style="claude", now_ts=now_ts)
    right_text = f"{reset_text} · {_fmt_pct(used_pct)}" if reset_text else _fmt_pct(used_pct)
    return {
        "id": win_id,
        "label": label,
        "used_pct": round(used_pct, 1),
        "remaining_pct": rem_pct,
        "bar_pct": round(used_pct, 1),
        "reset_text": reset_text,
        "right_text": right_text,
        "resets_at": resets_at,
        "expired": w["expired"],
        "state": _win_state(used_pct),
    }


def _parse_claude_oauth_payload(payload: Dict[str, Any], now_ts: Optional[float] = None) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    fh = payload.get("five_hour") or {}
    sd = payload.get("seven_day") or {}
    sd_opus = payload.get("seven_day_opus") or {}
    sd_sonnet = payload.get("seven_day_sonnet") or {}
    sd_fable = payload.get("seven_day_fable") or {}

    windows: List[Dict[str, Any]] = []
    seen_labels = set()

    w_five = _make_claude_window("five_hour", "5시간 제한", fh, now_ts=now_ts)
    if w_five:
        windows.append(w_five)
        seen_labels.add(w_five["label"])

    w_seven = _make_claude_window("seven_day", "주간 · 모든 모델", sd, now_ts=now_ts)
    if w_seven:
        windows.append(w_seven)
        seen_labels.add(w_seven["label"])

    # 1. Check explicit `limits` list if returned by API
    limits_list = payload.get("limits")
    if isinstance(limits_list, list):
        for idx, item in enumerate(limits_list):
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or item.get("type") or "").lower()
            scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
            model_obj = scope.get("model") if isinstance(scope.get("model"), dict) else {}
            model_name = str(
                model_obj.get("display_name")
                or model_obj.get("name")
                or item.get("display_name")
                or item.get("model")
                or item.get("label")
                or ""
            ).strip()
            if not model_name and ("five" in kind or "5h" in kind):
                lbl = "5시간 제한"
            elif not model_name and ("seven" in kind or "weekly" in kind):
                lbl = "주간 · 모든 모델"
            elif model_name:
                lbl = model_name if model_name.startswith("주간") else f"주간 · {model_name}"
            else:
                continue
            if lbl in seen_labels:
                continue
            win = _make_claude_window(f"limit_{idx}", lbl, item, now_ts=now_ts)
            if win:
                windows.append(win)
                seen_labels.add(lbl)

    # 2. Check model-scoped seven_day_* keys (seven_day_fable, seven_day_sonnet, seven_day_opus, etc.)
    if isinstance(sd_fable, dict) and sd_fable:
        lbl = "주간 · Fable"
        if lbl not in seen_labels:
            win = _make_claude_window("seven_day_fable", lbl, sd_fable, now_ts=now_ts)
            if win:
                windows.append(win)
                seen_labels.add(lbl)

    if isinstance(sd_sonnet, dict) and sd_sonnet:
        custom_name = str(sd_sonnet.get("display_name") or sd_sonnet.get("model_name") or "").strip()
        if custom_name:
            lbl = f"주간 · {custom_name}"
        elif "주간 · Fable" not in seen_labels:
            lbl = "주간 · Fable"
        else:
            lbl = "주간 · Sonnet"
        if lbl not in seen_labels:
            win = _make_claude_window("seven_day_sonnet", lbl, sd_sonnet, now_ts=now_ts)
            if win:
                windows.append(win)
                seen_labels.add(lbl)

    if isinstance(sd_opus, dict) and sd_opus:
        lbl = "주간 · Opus"
        if lbl not in seen_labels:
            win = _make_claude_window("seven_day_opus", lbl, sd_opus, now_ts=now_ts)
            if win:
                windows.append(win)
                seen_labels.add(lbl)

    for key, val in payload.items():
        if not key.startswith("seven_day_") or key in (
            "seven_day_fable",
            "seven_day_sonnet",
            "seven_day_opus",
            "seven_day_oauth_apps",
            "seven_day_cowork",
        ):
            continue
        if isinstance(val, dict) and val:
            suffix = key[len("seven_day_"):].replace("_", " ").strip().title()
            lbl = f"주간 · {suffix}"
            if lbl not in seen_labels:
                win = _make_claude_window(key, lbl, val, now_ts=now_ts)
                if win:
                    windows.append(win)
                    seen_labels.add(lbl)

    w5 = _window_pct(fh.get("utilization") if "utilization" in fh else fh.get("used_percentage"), fh.get("resets_at"), now_ts=now_ts)
    w7 = _window_pct(sd.get("utilization") if "utilization" in sd else sd.get("used_percentage"), sd.get("resets_at"), now_ts=now_ts)
    w_opus = _window_pct(sd_opus.get("utilization") if "utilization" in sd_opus else sd_opus.get("used_percentage"), sd_opus.get("resets_at"), now_ts=now_ts)
    fable_or_sonnet = sd_fable if (isinstance(sd_fable, dict) and sd_fable) else sd_sonnet
    w_sonnet = _window_pct(
        fable_or_sonnet.get("utilization") if "utilization" in fable_or_sonnet else fable_or_sonnet.get("used_percentage"),
        fable_or_sonnet.get("resets_at"),
        now_ts=now_ts,
    )

    return {
        "source": "claude-oauth",
        "primary_percent": w5["percent"],
        "primary_resets_at": fh.get("resets_at"),
        "secondary_percent": w7["percent"],
        "secondary_resets_at": sd.get("resets_at"),
        "opus_weekly_percent": w_opus["percent"],
        "sonnet_weekly_percent": w_sonnet["percent"],
        "windows": windows,
    }


def _fetch_claude_oauth_usage(
    force: bool = False,
    cred_path: Optional[Path] = None,
    now_ts: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Query Anthropic's OAuth usage endpoint for exact 5h / 7d / Fable / Opus utilization when signed in."""
    if os.environ.get("DIRECTOR_TESTING") == "1" and cred_path is None:
        return None
    now = time.monotonic()
    with _CLAUDE_API_LOCK:
        if not force and _CLAUDE_API_CACHE["data"] is not None and (now - _CLAUDE_API_CACHE["fetched_at"]) < _API_TTL_SEC:
            return _CLAUDE_API_CACHE["data"]
    token = _read_claude_oauth_token(cred_path)
    if not token:
        return None
    req = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-code/2.1.289",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        with _CLAUDE_API_LOCK:
            return _CLAUDE_API_CACHE["data"]

    parsed = _parse_claude_oauth_payload(payload, now_ts=now_ts) if isinstance(payload, dict) else None
    if parsed:
        with _CLAUDE_API_LOCK:
            _CLAUDE_API_CACHE["fetched_at"] = now
            _CLAUDE_API_CACHE["data"] = parsed
    return parsed


def _read_claude_local_usage(root: Optional[Path] = None, now_ts: Optional[float] = None) -> Dict[str, Any]:
    """Read recent (last 24h & 7d) per-model tokens and any rate_limits from ~/.claude/projects/**/*.jsonl."""
    base = root or _claude_root()
    out: Dict[str, Any] = {"by_model": {}, "rate_limits": None}
    if not base.exists():
        return out
    cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    try:
        files = sorted(base.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:25]
    except OSError:
        return out
    seen_ids = set()
    for path in files:
        try:
            size = path.stat().st_size
            with path.open("rb") as fh:
                if size > 524288:
                    fh.seek(size - 524288)
                raw = fh.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            if isinstance(obj.get("rate_limits"), dict) and out["rate_limits"] is None:
                rl = obj["rate_limits"]
                five = rl.get("five_hour") or rl.get("primary") or {}
                seven = rl.get("seven_day") or rl.get("secondary") or {}
                fable = rl.get("seven_day_fable") or rl.get("seven_day_sonnet") or {}
                parsed = _parse_claude_oauth_payload(
                    {"five_hour": five, "seven_day": seven, "seven_day_fable": fable},
                    now_ts=now_ts,
                )
                if parsed:
                    parsed["source"] = "claude-session"
                    out["rate_limits"] = parsed
            ts = str(obj.get("timestamp") or "")
            if ts and ts < cutoff_24h:
                continue
            msg = obj.get("message")
            if not isinstance(msg, dict):
                continue
            msg_id = msg.get("id") or obj.get("uuid")
            if msg_id and msg_id in seen_ids:
                continue
            if msg_id:
                seen_ids.add(msg_id)
            use = msg.get("usage")
            model_id = str(msg.get("model") or "").strip()
            if isinstance(use, dict) and model_id and not model_id.startswith("<"):
                tin = int(use.get("input_tokens") or 0) + int(use.get("cache_read_input_tokens") or 0) + int(use.get("cache_creation_input_tokens") or 0)
                tout = int(use.get("output_tokens") or 0)
                bucket = out["by_model"].setdefault(model_id, {"calls": 0, "tokens": 0})
                bucket["calls"] += 1
                bucket["tokens"] += tin + tout
    return out


def _search_ya29_in_text(text: str, depth: int = 0) -> Optional[str]:
    if not text or depth > 2:
        return None
    m = _YA29_RE.search(text)
    if m:
        return m.group(1)
    if depth >= 2:
        return None
    for chunk in _B64_CHUNK_RE.findall(text):
        if len(chunk) < 36:
            continue
        padded = chunk + ("=" * ((4 - len(chunk) % 4) % 4))
        for decoder in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                decoded_bytes = decoder(padded)
            except Exception:
                continue
            decoded_str = decoded_bytes.decode("utf-8", errors="ignore")
            found = _search_ya29_in_text(decoded_str, depth + 1)
            if found:
                return found
    return None


def _extract_agy_oauth_token(vscdb_path: Optional[Path] = None) -> Optional[str]:
    env_tok = os.environ.get("AGY_OAUTH_TOKEN", "").strip()
    if env_tok:
        return env_tok
    paths = [vscdb_path] if vscdb_path is not None else _antigravity_vscdb_candidates()
    for p in paths:
        if p is None or not p.exists():
            continue
        tmp_copy: Optional[Path] = None
        try:
            # Copy to a temp file to avoid SQLite locking conflicts when Antigravity IDE is open
            with tempfile.NamedTemporaryFile(prefix="agy_vscdb_", suffix=".sqlite", delete=False) as tf:
                tmp_copy = Path(tf.name)
            shutil.copy2(p, tmp_copy)
            with sqlite3.connect(str(tmp_copy), timeout=2.0) as conn:
                rows = conn.execute(
                    "SELECT key, value FROM ItemTable WHERE "
                    "key IN ('antigravityAuthStatus', 'antigravityUnifiedStateSync.oauthToken', 'uss-oauth', 'jetski-standalone-oauth-token') "
                    "OR key LIKE '%oauth%' OR key LIKE '%Auth%' OR key LIKE '%uss%' OR key LIKE '%jetski%'"
                ).fetchall()
                # Prioritize exact known keys first
                priority = {
                    "antigravityAuthStatus": 0,
                    "antigravityUnifiedStateSync.oauthToken": 1,
                    "uss-oauth": 2,
                    "jetski-standalone-oauth-token": 3,
                }
                rows.sort(key=lambda r: priority.get(str(r[0]), 99))
                for _, val in rows:
                    s_val = val.decode("utf-8", errors="ignore") if isinstance(val, bytes) else str(val or "")
                    tok = _search_ya29_in_text(s_val)
                    if tok:
                        return tok
        except Exception:
            pass
        finally:
            if tmp_copy is not None:
                try:
                    tmp_copy.unlink(missing_ok=True)
                except OSError:
                    pass
        # Raw byte fallback scan
        try:
            raw_bytes = p.read_bytes()
            m = re.search(rb"(ya29\.[A-Za-z0-9_\-\.]{25,})", raw_bytes)
            if m:
                return m.group(1).decode("ascii", errors="ignore")
        except Exception:
            pass
    return None


def _parse_antigravity_quota_payload(payload: Dict[str, Any], now_ts: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Parse Antigravity RetrieveUserQuotaSummary (groups/buckets) or GetUserStatus payload into UI quota windows."""
    if not isinstance(payload, dict):
        return None

    groups = (
        payload.get("groups")
        or (payload.get("quotaSummary") or {}).get("groups")
        or (payload.get("userQuotaSummary") or {}).get("groups")
        or (payload.get("summary") or {}).get("groups")
    )
    windows: List[Dict[str, Any]] = []

    if isinstance(groups, list) and groups:
        for grp in groups:
            if not isinstance(grp, dict):
                continue
            g_name = str(grp.get("displayName") or grp.get("groupId") or "").strip()
            g_low = g_name.lower()
            if "gemini" in g_low:
                g_prefix = "Gemini"
            elif "claude" in g_low or "gpt" in g_low or "3p" in g_low:
                g_prefix = "Claude/GPT"
            else:
                g_prefix = g_name or "모델"

            buckets = grp.get("buckets")
            if not isinstance(buckets, list):
                continue
            for b in buckets:
                if not isinstance(b, dict):
                    continue
                b_id = str(b.get("bucketId") or b.get("id") or "").strip()
                b_name = str(b.get("displayName") or b_id).strip()
                b_low = (b_id + " " + b_name).lower()
                if "weekly" in b_low or "7d" in b_low or "seven" in b_low:
                    w_type = "주간 한도"
                elif "5h" in b_low or "five hour" in b_low or "5 hour" in b_low:
                    w_type = "5시간 한도"
                else:
                    w_type = b_name

                desc = str(b.get("description") or "").strip()
                reset_time = b.get("resetTime") or b.get("resetsAt")

                rem_obj = b.get("remaining") if isinstance(b.get("remaining"), dict) else {}
                rem_frac = (
                    rem_obj.get("remainingFraction")
                    if "remainingFraction" in rem_obj
                    else b.get("remainingFraction")
                )
                if rem_frac is None and "remainingPercentage" in b:
                    try:
                        rem_frac = float(b["remainingPercentage"]) / 100.0
                    except (TypeError, ValueError):
                        rem_frac = None
                if rem_frac is None:
                    if "not used any" in desc.lower():
                        rem_frac = 1.0
                    else:
                        rem_frac = 0.0

                try:
                    rem_pct = round(max(0.0, min(100.0, float(rem_frac) * 100.0)), 1)
                except (TypeError, ValueError):
                    rem_pct = 100.0
                used_pct = round(max(0.0, 100.0 - rem_pct), 1)
                reset_text = _parse_agy_description_reset_ko(desc, reset_time_raw=reset_time, now_ts=now_ts)
                right_text = f"{reset_text} · {_fmt_pct(rem_pct)} 남음" if reset_text else f"{_fmt_pct(rem_pct)} 남음"

                windows.append({
                    "id": b_id or f"{g_prefix}_{w_type}",
                    "label": f"{g_prefix} · {w_type}",
                    "used_pct": used_pct,
                    "remaining_pct": rem_pct,
                    "bar_pct": rem_pct,
                    "reset_text": reset_text,
                    "right_text": right_text,
                    "resets_at": reset_time,
                    "expired": False,
                    "state": _win_state(used_pct),
                })

    # Fallback: GetUserStatus cascadeModelConfigData.clientModelConfigs
    if not windows:
        configs = (
            ((payload.get("userStatus") or {}).get("cascadeModelConfigData") or {}).get("clientModelConfigs")
            or payload.get("clientModelConfigs")
        )
        if isinstance(configs, list) and configs:
            pools: Dict[str, Dict[str, Any]] = {}
            for cfg in configs:
                if not isinstance(cfg, dict):
                    continue
                q_info = cfg.get("quotaInfo")
                if not isinstance(q_info, dict):
                    continue
                lbl = str(cfg.get("label") or "").lower()
                pool = "Gemini" if "gemini" in lbl else ("Claude/GPT" if ("claude" in lbl or "gpt" in lbl) else "모델")
                rem_f = q_info.get("remainingFraction", 0.0)
                try:
                    rem_p = round(max(0.0, min(100.0, float(rem_f) * 100.0)), 1)
                except (TypeError, ValueError):
                    rem_p = 100.0
                r_time = q_info.get("resetTime")
                cur = pools.get(pool)
                if cur is None or rem_p < cur["rem_pct"]:
                    pools[pool] = {"rem_pct": rem_p, "reset_time": r_time}
            for pool_name, info in pools.items():
                rem_pct = info["rem_pct"]
                used_pct = round(max(0.0, 100.0 - rem_pct), 1)
                reset_text = _format_reset_ko(info["reset_time"], style="agy", now_ts=now_ts)
                right_text = f"{reset_text} · {_fmt_pct(rem_pct)} 남음" if reset_text else f"{_fmt_pct(rem_pct)} 남음"
                windows.append({
                    "id": f"agy_{pool_name}",
                    "label": f"{pool_name} · 한도",
                    "used_pct": used_pct,
                    "remaining_pct": rem_pct,
                    "bar_pct": rem_pct,
                    "reset_text": reset_text,
                    "right_text": right_text,
                    "resets_at": info["reset_time"],
                    "expired": False,
                    "state": _win_state(used_pct),
                })

    if not windows:
        return None

    five_h_used = [w["used_pct"] for w in windows if "5시간" in w["label"]]
    weekly_used = [w["used_pct"] for w in windows if "주간" in w["label"]]
    all_used = [w["used_pct"] for w in windows]
    prim_pct = max(five_h_used) if five_h_used else (max(all_used) if all_used else 0.0)
    sec_pct = max(weekly_used) if weekly_used else None

    return {
        "source": "antigravity-quota",
        "primary_percent": prim_pct,
        "primary_resets_at": None,
        "primary_expired": False,
        "secondary_percent": sec_pct,
        "secondary_resets_at": None,
        "secondary_expired": False,
        "windows": windows,
    }


def _fetch_antigravity_cloudcode_quota(token: str, now_ts: Optional[float] = None) -> Optional[Dict[str, Any]]:
    endpoints = (
        "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
        "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
    )
    bodies = (b'{"project":"aicode-consumers"}', b"{}")
    for url in endpoints:
        for body in bodies:
            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": _AGY_USER_AGENT,
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                parsed = _parse_antigravity_quota_payload(payload, now_ts=now_ts)
                if parsed:
                    return parsed
            except Exception:
                continue
    return None


def _probe_antigravity_local_server(now_ts: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Fallback probe of a running local Antigravity Language Server on 127.0.0.1."""
    if os.name != "nt":
        return None
    creationflags = 0x08000000  # CREATE_NO_WINDOW
    try:
        ps_cmd = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -like 'language_server*' -or ($_.CommandLine -like '*csrf_token*' -and $_.CommandLine -like '*antigravity*') } | "
            "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"
        )
        proc_out = subprocess.check_output(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            timeout=2.5,
            creationflags=creationflags,
            stderr=subprocess.DEVNULL,
        ).decode("utf-8", errors="replace").strip()
        if not proc_out:
            return None
        items = json.loads(proc_out)
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            return None

        netstat_out = subprocess.check_output(
            ["netstat", "-ano", "-p", "tcp"],
            timeout=2.0,
            creationflags=creationflags,
            stderr=subprocess.DEVNULL,
        ).decode("utf-8", errors="replace")

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        for item in items:
            pid = str(item.get("ProcessId") or "").strip()
            cmdline = str(item.get("CommandLine") or "")
            m_csrf = re.search(r"--csrf_token[=\s]+([A-Za-z0-9_\-]+)", cmdline)
            if not pid or not m_csrf:
                continue
            csrf = m_csrf.group(1)
            ports: List[int] = []
            for line in netstat_out.splitlines():
                if "LISTENING" in line and line.strip().endswith(pid):
                    m_port = re.search(r"127\.0\.0\.1:(\d+)", line)
                    if m_port:
                        ports.append(int(m_port.group(1)))
            for port in ports[:4]:
                for method_name in ("RetrieveUserQuotaSummary", "GetUserStatus"):
                    for scheme in ("https", "http"):
                        url = f"{scheme}://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/{method_name}"
                        req = urllib.request.Request(
                            url,
                            data=b"{}",
                            headers={
                                "Content-Type": "application/json",
                                "Connect-Protocol-Version": "1",
                                "X-Codeium-Csrf-Token": csrf,
                            },
                            method="POST",
                        )
                        try:
                            kwargs: Dict[str, Any] = {"timeout": 1.2}
                            if scheme == "https":
                                kwargs["context"] = ctx
                            with urllib.request.urlopen(req, **kwargs) as resp:
                                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                            parsed = _parse_antigravity_quota_payload(payload, now_ts=now_ts)
                            if parsed:
                                parsed["source"] = "antigravity-local-ls"
                                return parsed
                        except Exception:
                            continue
    except Exception:
        pass
    return None


def _fetch_antigravity_quota(
    force: bool = False,
    vscdb_path: Optional[Path] = None,
    now_ts: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch live Antigravity quota bars (Gemini Models & Claude/GPT models, Weekly + 5h)."""
    if os.environ.get("DIRECTOR_TESTING") == "1" and vscdb_path is None:
        return None
    now = time.monotonic()
    with _AGY_API_LOCK:
        if not force and _AGY_API_CACHE["data"] is not None and (now - _AGY_API_CACHE["fetched_at"]) < _API_TTL_SEC:
            return _AGY_API_CACHE["data"]

    parsed: Optional[Dict[str, Any]] = None
    token = _extract_agy_oauth_token(vscdb_path)
    if token:
        parsed = _fetch_antigravity_cloudcode_quota(token, now_ts=now_ts)
    if parsed is None and vscdb_path is None:
        parsed = _probe_antigravity_local_server(now_ts=now_ts)

    if parsed:
        with _AGY_API_LOCK:
            _AGY_API_CACHE["fetched_at"] = now
            _AGY_API_CACHE["data"] = parsed
        return parsed

    with _AGY_API_LOCK:
        return _AGY_API_CACHE["data"]


def _db_usage_and_health() -> Dict[str, Any]:
    db_path = get_db_path()
    cutoff_24h = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    by_provider: Dict[str, Dict[str, Any]] = {}
    health: Dict[str, Dict[str, Any]] = {}
    if not db_path.exists():
        return {"by_provider": by_provider, "health": health}
    try:
        with sqlite3.connect(str(db_path), timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "ai_usage" in tables:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_usage)").fetchall()}
                model_expr = "COALESCE(NULLIF(model, ''), 'CLI 기본값')" if "model" in cols else "'CLI 기본값'"
                rows = conn.execute(
                    f"SELECT provider, {model_expr} AS model_name, "
                    "COUNT(*) AS calls, "
                    "COALESCE(SUM(tokens_total), 0) AS tokens, "
                    "COALESCE(SUM(duration_sec), 0) AS seconds, "
                    "SUM(CASE WHEN status!='OK' THEN 1 ELSE 0 END) AS errors "
                    "FROM ai_usage WHERE status!='RUNNING' AND started_at >= ? "
                    "GROUP BY provider, model_name ORDER BY tokens DESC",
                    (cutoff_24h,),
                ).fetchall()
                for r in rows:
                    prov = model_settings.DOMAIN_TO_PROVIDER.get(r["provider"])
                    if not prov:
                        continue
                    p_bucket = by_provider.setdefault(prov, {"calls": 0, "tokens": 0, "seconds": 0.0, "models": []})
                    p_bucket["calls"] += int(r["calls"] or 0)
                    p_bucket["tokens"] += int(r["tokens"] or 0)
                    p_bucket["seconds"] += float(r["seconds"] or 0.0)
                    p_bucket["models"].append({
                        "model": r["model_name"],
                        "calls": int(r["calls"] or 0),
                        "tokens": int(r["tokens"] or 0),
                        "seconds": round(float(r["seconds"] or 0.0), 1),
                        "errors": int(r["errors"] or 0),
                    })
            if "provider_health" in tables:
                for r in conn.execute("SELECT provider, status, cooldown_until, last_error_type FROM provider_health").fetchall():
                    prov = model_settings.DOMAIN_TO_PROVIDER.get(r["provider"])
                    if prov:
                        health[prov] = {
                            "status": r["status"],
                            "cooldown_until": r["cooldown_until"],
                            "last_error_type": r["last_error_type"],
                        }
    except sqlite3.Error:
        pass
    return {"by_provider": by_provider, "health": health}


def snapshot(
    codex_dir: Optional[Path] = None,
    claude_dir: Optional[Path] = None,
    claude_cred_path: Optional[Path] = None,
    codex_auth_path: Optional[Path] = None,
    agy_vscdb_path: Optional[Path] = None,
    force: bool = False,
    now_ts: Optional[float] = None,
) -> Dict[str, Any]:
    """Return real-time per-provider & per-model usage + rate-limit snapshot for the UI."""
    models = model_settings.load()
    efforts = model_settings.load_efforts()
    db_info = _db_usage_and_health()
    codex_wham_rl = _fetch_codex_wham_usage(force=force, auth_path=codex_auth_path, now_ts=now_ts)
    codex_local_rl = _read_codex_rate_limits(codex_dir, now_ts=now_ts)
    claude_local = _read_claude_local_usage(claude_dir, now_ts=now_ts)
    claude_oauth_rl = _fetch_claude_oauth_usage(force=force, cred_path=claude_cred_path, now_ts=now_ts)
    agy_rl = _fetch_antigravity_quota(force=force, vscdb_path=agy_vscdb_path, now_ts=now_ts)

    providers: Dict[str, Dict[str, Any]] = {}
    for prov in model_settings.PROVIDERS:
        db_p = db_info["by_provider"].get(prov, {"calls": 0, "tokens": 0, "seconds": 0.0, "models": []})
        hp = db_info["health"].get(prov, {})
        hp_status = str(hp.get("status") or "ACTIVE")
        rl = None
        model_rows = list(db_p["models"])
        total_calls = int(db_p["calls"])
        total_tokens = int(db_p["tokens"])

        if prov == "openai":
            rl = codex_wham_rl or codex_local_rl
        elif prov == "claude":
            rl = claude_oauth_rl or claude_local.get("rate_limits")
            existing = {m["model"]: m for m in model_rows}
            for m_id, stats in claude_local.get("by_model", {}).items():
                if m_id not in existing:
                    model_rows.append({
                        "model": m_id,
                        "calls": stats["calls"],
                        "tokens": stats["tokens"],
                        "seconds": 0.0,
                        "errors": 0,
                        "source": "claude-session",
                    })
                    total_calls += int(stats["calls"])
                    total_tokens += int(stats["tokens"])
        elif prov == "antigravity":
            rl = agy_rl

        pct_candidates: List[float] = []
        if rl:
            for k in ("primary_percent", "secondary_percent", "opus_weekly_percent", "sonnet_weekly_percent"):
                v = rl.get(k)
                if v is not None:
                    pct_candidates.append(float(v))
            for w in rl.get("windows") or []:
                if isinstance(w, dict) and w.get("used_pct") is not None:
                    pct_candidates.append(float(w["used_pct"]))
        max_pct = max(pct_candidates) if pct_candidates else None

        if hp_status == "QUOTA_EXCEEDED":
            state = "exhausted"
        elif max_pct is not None and max_pct >= 95:
            state = "exhausted"
        elif max_pct is not None and max_pct >= 75:
            state = "warning"
        elif hp_status == "ACTIVE":
            state = "ok"
        else:
            state = "warning"

        providers[prov] = {
            "model": models.get(prov, "") or "CLI 기본값",
            "effort": efforts.get(prov, "") or "기본값",
            "today_calls": total_calls,
            "today_tokens": total_tokens,
            "today_seconds": round(db_p["seconds"], 1),
            "by_model": model_rows,
            "rate_limits": rl,
            "health": hp_status,
            "cooldown_until": hp.get("cooldown_until"),
            "state": state,
        }

    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "providers": providers,
    }
