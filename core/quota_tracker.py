"""Per-model & per-provider usage + live CLI rate-limit/quota tracker for the UI."""
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core import model_settings
from core.db_path import get_db_path

_CLAUDE_API_LOCK = threading.Lock()
_CLAUDE_API_CACHE: Dict[str, Any] = {"fetched_at": 0.0, "data": None}
_CLAUDE_API_TTL_SEC = 180.0


def _codex_root() -> Path:
    return Path.home() / ".codex" / "sessions"


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


def _window_pct(used_raw: Any, resets_at_raw: Any, now_ts: Optional[float] = None) -> Dict[str, Any]:
    """Return normalized percent & expired status. If resets_at is in the past, percent is 0.0 (window reset)."""
    now = now_ts if now_ts is not None else time.time()
    pct = _num_or_none(used_raw)
    reset_ts = _parse_epoch_seconds(resets_at_raw)
    if reset_ts is not None and reset_ts <= now:
        return {"percent": 0.0, "resets_at": resets_at_raw, "expired": True}
    return {"percent": pct, "resets_at": resets_at_raw, "expired": False}


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
                return {
                    "source": "codex-session",
                    "primary_percent": w_prim["percent"],
                    "primary_resets_at": prim.get("resets_at"),
                    "primary_expired": w_prim["expired"],
                    "secondary_percent": w_sec["percent"],
                    "secondary_resets_at": sec.get("resets_at"),
                    "secondary_expired": w_sec["expired"],
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


def _fetch_claude_oauth_usage(
    force: bool = False,
    cred_path: Optional[Path] = None,
    now_ts: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Query Anthropic's OAuth usage endpoint for exact 5h / 7d / Opus / Sonnet utilization when signed in."""
    if os.environ.get("DIRECTOR_TESTING") == "1" and cred_path is None:
        return None
    now = time.monotonic()
    with _CLAUDE_API_LOCK:
        if not force and _CLAUDE_API_CACHE["data"] is not None and (now - _CLAUDE_API_CACHE["fetched_at"]) < _CLAUDE_API_TTL_SEC:
            return _CLAUDE_API_CACHE["data"]
    token = _read_claude_oauth_token(cred_path)
    if not token:
        return None
    req = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-code/2.1.90",
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

    if not isinstance(payload, dict):
        return None
    fh = payload.get("five_hour") or {}
    sd = payload.get("seven_day") or {}
    sd_opus = payload.get("seven_day_opus") or {}
    sd_sonnet = payload.get("seven_day_sonnet") or {}
    w5 = _window_pct(fh.get("utilization") if "utilization" in fh else fh.get("used_percentage"), fh.get("resets_at"), now_ts=now_ts)
    w7 = _window_pct(sd.get("utilization") if "utilization" in sd else sd.get("used_percentage"), sd.get("resets_at"), now_ts=now_ts)
    w_opus = _window_pct(sd_opus.get("utilization") if "utilization" in sd_opus else sd_opus.get("used_percentage"), sd_opus.get("resets_at"), now_ts=now_ts)
    w_sonnet = _window_pct(sd_sonnet.get("utilization") if "utilization" in sd_sonnet else sd_sonnet.get("used_percentage"), sd_sonnet.get("resets_at"), now_ts=now_ts)

    parsed = {
        "source": "claude-oauth",
        "primary_percent": w5["percent"],
        "primary_resets_at": fh.get("resets_at"),
        "secondary_percent": w7["percent"],
        "secondary_resets_at": sd.get("resets_at"),
        "opus_weekly_percent": w_opus["percent"],
        "sonnet_weekly_percent": w_sonnet["percent"],
    }
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
                w5 = _window_pct(five.get("used_percentage") if "used_percentage" in five else five.get("used_percent"), five.get("resets_at"), now_ts=now_ts)
                w7 = _window_pct(seven.get("used_percentage") if "used_percentage" in seven else seven.get("used_percent"), seven.get("resets_at"), now_ts=now_ts)
                out["rate_limits"] = {
                    "source": "claude-session",
                    "primary_percent": w5["percent"],
                    "primary_resets_at": five.get("resets_at"),
                    "secondary_percent": w7["percent"],
                    "secondary_resets_at": seven.get("resets_at"),
                }
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


def _num_or_none(v: Any) -> Optional[float]:
    try:
        return round(float(v), 1) if v is not None else None
    except (TypeError, ValueError):
        return None


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
    force: bool = False,
    now_ts: Optional[float] = None,
) -> Dict[str, Any]:
    """Return real-time per-provider & per-model usage + rate-limit snapshot for the UI."""
    models = model_settings.load()
    efforts = model_settings.load_efforts()
    db_info = _db_usage_and_health()
    codex_rl = _read_codex_rate_limits(codex_dir, now_ts=now_ts)
    claude_local = _read_claude_local_usage(claude_dir, now_ts=now_ts)
    claude_oauth_rl = _fetch_claude_oauth_usage(force=force, cred_path=claude_cred_path, now_ts=now_ts)

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
            rl = codex_rl
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

        pct = rl.get("primary_percent") if rl else None
        sec_pct = rl.get("secondary_percent") if rl else None
        opus_pct = rl.get("opus_weekly_percent") if rl else None
        max_pct = max([p for p in (pct, sec_pct, opus_pct) if p is not None], default=None)

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
