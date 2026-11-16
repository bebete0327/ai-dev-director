"""Export and import 3-provider CLI credentials (Claude, Codex, Antigravity) between PC and Linux Cloud VM.

Usage:
  1. On Windows PC (where Claude, Codex, Antigravity are already logged in):
     python scripts/auth_sync.py export
     -> Creates data/auth_bundle.json

  2. On Linux Cloud Server (after copying data/auth_bundle.json):
     python3 scripts/auth_sync.py import
     -> Restores ~/.claude/.credentials.json, ~/.codex/auth.json, and ~/.config/Antigravity/User/globalStorage/state.vscdb
"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import quota_tracker


def export_bundle(out_path: Path) -> int:
    bundle = {}

    claude_cred = quota_tracker._claude_credentials_path()
    if claude_cred.exists():
        try:
            bundle["claude_credentials"] = json.loads(claude_cred.read_text(encoding="utf-8"))
        except Exception:
            pass

    codex_auth = quota_tracker._codex_auth_path()
    if codex_auth.exists():
        try:
            bundle["codex_auth"] = json.loads(codex_auth.read_text(encoding="utf-8"))
        except Exception:
            pass

    agy_tok = quota_tracker._extract_agy_oauth_token()
    if agy_tok:
        bundle["agy_oauth_token"] = agy_tok

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(bundle, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        out_path.chmod(0o600)
    except OSError:
        pass
    print(f"[OK] 인증 번들 내보내기 완료: {out_path} (포함된 항목: {', '.join(bundle.keys()) or '없음'})")
    return 0


def import_bundle(in_path: Path) -> int:
    if not in_path.exists():
        print(f"[오류] 번들 파일이 없습니다: {in_path}", file=sys.stderr)
        return 1
    bundle = json.loads(in_path.read_text(encoding="utf-8"))
    restored = []

    if isinstance(bundle.get("claude_credentials"), dict):
        c_path = quota_tracker._claude_credentials_path()
        c_path.parent.mkdir(parents=True, exist_ok=True)
        c_path.write_text(json.dumps(bundle["claude_credentials"], indent=2), encoding="utf-8")
        try:
            c_path.chmod(0o600)
        except OSError:
            pass
        restored.append(f"Claude ({c_path})")

    if isinstance(bundle.get("codex_auth"), dict):
        x_path = quota_tracker._codex_auth_path()
        x_path.parent.mkdir(parents=True, exist_ok=True)
        x_path.write_text(json.dumps(bundle["codex_auth"], indent=2), encoding="utf-8")
        try:
            x_path.chmod(0o600)
        except OSError:
            pass
        restored.append(f"Codex ({x_path})")

    agy_tok = str(bundle.get("agy_oauth_token") or "").strip()
    if agy_tok:
        vscdb = Path.home() / ".config" / "Antigravity" / "User" / "globalStorage" / "state.vscdb"
        vscdb.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(vscdb)) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS ItemTable (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute(
                "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
                ("antigravityAuthStatus", json.dumps({"accessToken": agy_tok})),
            )
        restored.append(f"Antigravity ({vscdb})")

    print(f"[OK] 클라우드 인증 복원 완료: {', '.join(restored) or '없음'}")
    return 0


def main() -> int:
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "export").strip().lower()
    target = Path(sys.argv[2]) if len(sys.argv) > 2 else (ROOT / "data" / "auth_bundle.json")
    if cmd == "export":
        return export_bundle(target)
    if cmd == "import":
        return import_bundle(target)
    print("사용법: python scripts/auth_sync.py [export|import] [파일경로]")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
