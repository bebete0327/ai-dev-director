"""
Canonical DB path resolver for AI DEV DIRECTOR.

Every component (StateMachine, ChatManager, ProviderManager, dashboard) must obtain the
SQLite path through get_db_path(). The path is absolute and anchored at the project source
root, so running the dashboard or CLI from a different cwd can never create a split-brain DB.

Test isolation: when DIRECTOR_TESTING=1 and TEST_DB_PATH is set, that path is returned instead.
"""
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Optional


def resolve_bundle_root() -> Path:
    """Read-only asset root: PyInstaller _MEIPASS when frozen, otherwise repository root."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass).resolve()
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resolve_source_root() -> Path:
    """Mutable runtime root (config.yaml, companion.json, data/, runs/).
    In frozen mode: portable folder next to the .exe when config.yaml/companion.json sits beside it,
    or %LOCALAPPDATA%/AI-Dev-Director when a standalone .exe is dropped onto Desktop/Downloads alone."""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if (exe_dir / "config.yaml").exists() or (exe_dir / "companion.json").exists():
            return exe_dir
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".ai-dev-director"))
        return (base / "AI-Dev-Director").resolve()
    return Path(__file__).resolve().parent.parent


def ensure_runtime_files(root: Optional[Path] = None, bundle: Optional[Path] = None) -> Path:
    """Ensure mutable runtime directories and default config files exist for standalone launches."""
    target = (root or SOURCE_ROOT).resolve()
    assets = (bundle or BUNDLE_ROOT).resolve()
    (target / "data").mkdir(parents=True, exist_ok=True)
    for name in ("config.yaml", "companion.json"):
        dest = target / name
        if not dest.exists():
            src = assets / name
            if src.exists() and src.resolve() != dest.resolve():
                shutil.copyfile(src, dest)
            elif name == "companion.json":
                dest.write_text(json.dumps({"port": 8081, "allowed_projects": []}, indent=2) + "\n", encoding="utf-8")
    return target


SOURCE_ROOT = resolve_source_root()
BUNDLE_ROOT = resolve_bundle_root()
DEFAULT_DB_PATH = SOURCE_ROOT / "data" / "director.db"


def get_db_path() -> Path:
    if os.environ.get("DIRECTOR_TESTING") == "1" and os.environ.get("TEST_DB_PATH"):
        return Path(os.environ["TEST_DB_PATH"]).resolve()
    return SOURCE_ROOT / "data" / "director.db"


def get_lock_dir() -> Path:
    """Directory for cross-process lock files (next to the DB; isolated under TEST_DB_PATH in tests)."""
    return get_db_path().parent / "locks"
