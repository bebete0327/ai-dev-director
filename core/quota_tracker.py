"""Local, zero-network per-model usage & CLI rate-limit/quota aggregator for the UI."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core import model_settings
from core.db_path import get_db_path


def _codex_root() -> Path:
    return Path.home() / ".codex" / "sessions"


def _claude_root() -> Path:
    return Path.home() / ".claude" / "projects"


def _read_codex_rate_limits(root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Read the newest rate_limits block from ~/.codex/sessions/**/rollout-*.jsonl (tail-only for speed)."""
    base = root or _codex_root()
    if not base.exists():
        return None
    try:
        files = sorted(base.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
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
                return {
                    "source": "codex-session",
                    "primary_percent": _num_or_none(prim.get("used_percent")),
                    "primary_resets_at": prim.get("resets_at"),
                    "secondary_percent": _num_or_none(sec.get("used_percent")),
                    "secondary_resets_at": sec.get("resets_at"),
                }
    return None


def _read_claude_local_usage(root: Optional[Path] = None) -> Dict[str, Any]:
    """Read today's per-model tokens and any rate_limits from recent ~/.claude/projects/**/*.jsonl."""
    base = root or _claude_root()
    out: Dict[str, Any] = {"by_model": {}, "rate_limits": None}
    if not base.exists():
        return out
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        files = sorted(base.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:8]
    except OSError:
        return out
    seen_ids = set()
    for path in files:
        try:
            size = path.stat().st_size
            with path.open("rb") as fh:
                if size > 262144:
                    fh.seek(size - 262144)
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
                out["rate_limits"] = {
                    "source": "claude-session",
                    "primary_percent": _num_or_none(five.get("used_percentage") or five.get("used_percent")),
                    "primary_resets_at": five.get("resets_at"),
                    "secondary_percent": _num_or_none(seven.get("used_percentage") or seven.get("used_percent")),
                    "secondary_resets_at": seven.get("resets_at"),
                }
            ts = str(obj.get("timestamp") or "")
            if ts and not ts.startswith(today_str):
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
            if isinstance(use, dict) and model_id:
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
    today_prefix = datetime.utcnow().strftime("%Y-%m-%d")
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
                    (today_prefix,),
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
) -> Dict[str, Any]:
    """Return real-time per-provider & per-model usage + rate-limit snapshot for the UI."""
    models = model_settings.load()
    efforts = model_settings.load_efforts()
    db_info = _db_usage_and_health()
    codex_rl = _read_codex_rate_limits(codex_dir)
    claude_local = _read_claude_local_usage(claude_dir)

    providers: Dict[str, Dict[str, Any]] = {}
    for prov in model_settings.PROVIDERS:
        db_p = db_info["by_provider"].get(prov, {"calls": 0, "tokens": 0, "seconds": 0.0, "models": []})
        hp = db_info["health"].get(prov, {})
        hp_status = str(hp.get("status") or "ACTIVE")
        rl = None
        model_rows = list(db_p["models"])

        if prov == "openai":
            rl = codex_rl
        elif prov == "claude":
            rl = claude_local.get("rate_limits")
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

        pct = rl.get("primary_percent") if rl else None
        if hp_status == "QUOTA_EXCEEDED":
            state = "exhausted"
        elif pct is not None and pct >= 95:
            state = "exhausted"
        elif pct is not None and pct >= 75:
            state = "warning"
        elif hp_status == "ACTIVE":
            state = "ok"
        else:
            state = "warning"

        providers[prov] = {
            "model": models.get(prov, "") or "CLI 기본값",
            "effort": efforts.get(prov, "") or "기본값",
            "today_calls": db_p["calls"],
            "today_tokens": db_p["tokens"],
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
