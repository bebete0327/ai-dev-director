#!/usr/bin/env python3
"""Windowless desktop entrypoint for AI DEV DIRECTOR standalone executable (AI-Dev-Director.exe)."""
import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path
from typing import List, Optional


def _show_error_dialog(title: str, message: str) -> None:
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, str(message), str(title), 0x00000010)
            return
        except Exception:
            pass
    try:
        if sys.stderr is not None:
            sys.stderr.write(f"[{title}] {message}\n")
    except Exception:
        pass


def _redirect_null_streams(data_dir: Path) -> None:
    """Under PyInstaller --noconsole on Windows, sys.stdout/sys.stderr are None, which would crash
    http.server.BaseHTTPRequestHandler.log_message or print(flush=True). Route them to data/*.log."""
    data_dir.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or getattr(sys, "frozen", False):
        sys.stdout = open(data_dir / "companion.log", "a", encoding="utf-8", buffering=1)
    if sys.stderr is None or getattr(sys, "frozen", False):
        sys.stderr = open(data_dir / "companion.err", "a", encoding="utf-8", buffering=1)


def is_companion_alive(port: int, timeout: float = 1.5) -> bool:
    conn = None
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request("GET", "/ops/state")
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            return False
        payload = json.loads(body.decode("utf-8", errors="replace"))
        return isinstance(payload, dict) and "solomon_url" in payload
    except Exception:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _find_chromium_app_browser() -> Optional[str]:
    """Locate Edge or Chrome on Windows so the console opens as a clean standalone app window (--app=URL)."""
    if os.name != "nt":
        return None
    roots = [
        os.environ.get("ProgramFiles(x86)", ""),
        os.environ.get("ProgramFiles", ""),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    rel_candidates = [
        "Microsoft/Edge/Application/msedge.exe",
        "Google/Chrome/Application/chrome.exe",
    ]
    for base in roots:
        if not base:
            continue
        for rel in rel_candidates:
            cand = Path(base) / rel
            if cand.is_file():
                return str(cand)
    for name in ("msedge", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def open_app_window(url: str) -> None:
    browser = _find_chromium_app_browser()
    if browser:
        try:
            kwargs = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)} if os.name == "nt" else {}
            subprocess.Popen([browser, f"--app={url}"], shell=False, **kwargs)
            return
        except Exception:
            pass
    webbrowser.open(url)


def _open_when_ready(port: int, url: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_companion_alive(port, timeout=1.0):
            open_app_window(url)
            return
        time.sleep(0.15)


def run_smoke_test() -> int:
    """Self-test for packaged .exe builds: boots CompanionHandler on an ephemeral port, verifies UI + /ops/state + shutdown."""
    from types import SimpleNamespace
    from core.db_path import SOURCE_ROOT, BUNDLE_ROOT, ensure_runtime_files
    import companion_server
    import dashboard_server as d

    ensure_runtime_files(SOURCE_ROOT, BUNDLE_ROOT)
    d._ensure_schema()
    server = d.DashboardServer(("127.0.0.1", 0), companion_server.CompanionHandler)
    server.bridge_token = "smoke-test-token"
    server.stopping = False
    server.bridge = SimpleNamespace(submit=lambda p: {"status": "submitted"}, list=lambda: [], allowed=set())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/")
        r1 = conn.getresponse()
        html = r1.read()
        conn.close()
        if r1.status != 200 or b"AI DEV DIRECTOR" not in html:
            raise RuntimeError(f"Unexpected root response: {r1.status}")
        if not is_companion_alive(port, timeout=3.0):
            raise RuntimeError("/ops/state check failed")
        conn2 = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn2.request("POST", "/ops/shutdown", headers={"X-Solomon-Bridge": "smoke-test-token"})
        r2 = conn2.getresponse()
        r2.read()
        conn2.close()
        if r2.status != 200:
            raise RuntimeError(f"Shutdown returned {r2.status}")
        thread.join(timeout=5)
        print(f"SMOKE_OK port={port}")
        return 0
    finally:
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    # 1. Internal native folder-picker helper mode (invoked by companion_server.pick_folder when frozen)
    if args and args[0] == "--pick-folder":
        from scripts.pick_folder import main as pick_folder_main
        sys.argv = [sys.argv[0]] + args[1:]
        return int(pick_folder_main())

    # 2. Automated CI / packaging smoke-test mode
    if args and args[0] == "--smoke-test":
        return run_smoke_test()

    # 3. Optional CLI pass-through: AI-Dev-Director.exe --cli status
    if args and args[0] == "--cli":
        import director
        sys.argv = [sys.argv[0]] + args[1:]
        director.cli()
        return 0

    from core.db_path import SOURCE_ROOT, BUNDLE_ROOT, ensure_runtime_files
    root = ensure_runtime_files(SOURCE_ROOT, BUNDLE_ROOT)
    _redirect_null_streams(root / "data")

    try:
        cfg = json.loads((root / "companion.json").read_text(encoding="utf-8"))
        port = int(cfg.get("port", 8081))
    except Exception:
        port = 8081
    url = f"http://127.0.0.1:{port}"

    # Single-instance check: if already running, simply open the UI window and exit cleanly.
    if is_companion_alive(port):
        open_app_window(url)
        return 0

    pid_file = root / "data" / "companion.pid"
    try:
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass

    threading.Thread(target=_open_when_ready, args=(port, url), name="director-open-ui", daemon=True).start()

    try:
        import companion_server
        companion_server.run()
        return 0
    except OSError as exc:
        _show_error_dialog(
            "AI DEV DIRECTOR 실행 오류",
            f"{port} 포트를 열지 못했습니다.\n다른 프로그램이 {port} 포트를 사용 중인지 확인해 주세요.\n\n상세 오류: {exc}\n로그 위치: {root / 'data' / 'companion.err'}",
        )
        return 1
    except Exception as exc:
        traceback.print_exc()
        _show_error_dialog(
            "AI DEV DIRECTOR 실행 오류",
            f"예기치 않은 오류가 발생했습니다: {exc}\n\n로그 위치: {root / 'data' / 'companion.err'}",
        )
        return 1
    finally:
        try:
            if pid_file.exists() and pid_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_file.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
