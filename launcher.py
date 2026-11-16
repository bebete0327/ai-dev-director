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


_IS_WINDOWS = os.name == "nt"


def _show_error_dialog(title: str, message: str) -> None:
    if _IS_WINDOWS:
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


def _strip_zone_identifier(target: Path) -> bool:
    """Remove NTFS Mark-of-the-Web (:Zone.Identifier) alternate data stream on Windows so Explorer
    stops prompting 'Open File - Security Warning (untrusted publisher)' on subsequent launches."""
    if not _IS_WINDOWS:
        return False
    ads_path = f"{target}:Zone.Identifier"
    try:
        import ctypes
        if ctypes.windll.kernel32.DeleteFileW(str(ads_path)):
            return True
    except Exception:
        pass
    try:
        os.remove(ads_path)
        return True
    except OSError:
        return False


def unblock_windows_files(root: Path) -> int:
    """Best-effort removal of Zone.Identifier ADS from the executable and local scripts."""
    if not _IS_WINDOWS:
        return 0
    removed = 0
    targets = set()
    if getattr(sys, "frozen", False) and sys.executable:
        targets.add(Path(sys.executable))
    try:
        for p in root.iterdir():
            if p.is_file() and p.suffix.lower() in {".exe", ".dll", ".ps1", ".cmd", ".bat"}:
                targets.add(p)
    except OSError:
        pass
    for target in targets:
        if _strip_zone_identifier(target):
            removed += 1
    return removed


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
    if not _IS_WINDOWS:
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


def open_app_window(url: str, profile_dir: Optional[Path] = None) -> Optional[subprocess.Popen]:
    """Launch an isolated Edge/Chrome --app window and return its Popen handle so closing the window
    can automatically shut down the server. Falls back to default browser if neither is found."""
    browser = _find_chromium_app_browser()
    if browser:
        try:
            cmd = [browser, f"--app={url}", "--no-first-run", "--no-default-browser-check",
                   "--disable-background-mode", "--disable-extensions", "--window-size=1460,920"]
            if profile_dir is not None:
                profile_dir.mkdir(parents=True, exist_ok=True)
                cmd.append(f"--user-data-dir={profile_dir}")
            kwargs = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)} if _IS_WINDOWS else {}
            return subprocess.Popen(cmd, shell=False, **kwargs)
        except Exception:
            pass
    webbrowser.open(url)
    return None


def _shutdown_all(server, win_proc: Optional[subprocess.Popen] = None) -> None:
    """Stop active CLI child processes, shut down the HTTP server, and close the app window if still open."""
    server.stopping = True
    try:
        from core.process_runner import ProcessTreeRunner
        ProcessTreeRunner.kill_active()
    except Exception:
        pass
    if win_proc is not None and win_proc.poll() is None:
        try:
            from core.process_runner import ProcessTreeRunner
            ProcessTreeRunner.kill_process_tree(win_proc.pid)
        except Exception:
            try:
                win_proc.terminate()
            except Exception:
                pass
    try:
        server.shutdown()
    except Exception:
        pass


def run_desktop_session(
    server,
    url: str,
    profile_dir: Path,
    startup_handoff_sec: float = 2.0,
    idle_grace_sec: float = 18.0,
    poll_interval: float = 0.25,
) -> None:
    """Run HTTP server and tie its lifecycle to the desktop app window:
    - Closing the window (X button) automatically stops the server and active CLI child processes.
    - Clicking '시스템 중지' in the UI automatically closes the app window and exits."""
    port = server.server_address[1]
    server_thread = threading.Thread(target=server.serve_forever, name="director-http", daemon=True)
    server_thread.start()
    try:
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            if is_companion_alive(port, timeout=1.0):
                break
            time.sleep(0.1)

        win_proc = open_app_window(url, profile_dir=profile_dir)
        launched_at = time.monotonic()
        dedicated_window = False

        while server_thread.is_alive() and not getattr(server, "stopping", False):
            if win_proc is not None:
                alive = win_proc.poll() is None
                if alive and (time.monotonic() - launched_at) >= startup_handoff_sec:
                    dedicated_window = True
                if not alive:
                    if dedicated_window:
                        print("AI DEV window closed by user -> shutting down", flush=True)
                        break
                    # Browser process handed off immediately to an existing broker; switch to poll-lease mode.
                    win_proc = None
            else:
                if getattr(server, "had_client_contact", False):
                    idle_for = time.monotonic() - getattr(server, "last_client_contact", time.monotonic())
                    if idle_for >= idle_grace_sec:
                        print("AI DEV browser tab closed (idle timeout) -> shutting down", flush=True)
                        break
            time.sleep(poll_interval)
    finally:
        _shutdown_all(server, win_proc if "win_proc" in locals() else None)
        server_thread.join(timeout=4.0)
        try:
            server.server_close()
        except Exception:
            pass
        print("AI DEV companion stopped", flush=True)


def run_smoke_test() -> int:
    """Self-test for packaged .exe builds: boots CompanionHandler on an ephemeral port, verifies UI + /ops/models + shutdown."""
    from types import SimpleNamespace
    from core.db_path import SOURCE_ROOT, BUNDLE_ROOT, ensure_runtime_files
    import scripts.pick_folder as _pf
    import companion_server
    import dashboard_server as d

    if not callable(getattr(_pf, "main", None)):
        raise RuntimeError("scripts.pick_folder.main missing")

    ensure_runtime_files(SOURCE_ROOT, BUNDLE_ROOT)
    unblock_windows_files(SOURCE_ROOT)
    d._ensure_schema()
    server = d.DashboardServer(("127.0.0.1", 0), companion_server.CompanionHandler)
    server.bridge_token = "smoke-test-token"
    server.stopping = False
    server.had_client_contact = False
    server.last_client_contact = time.monotonic()
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
        if r1.status != 200 or b"AI DEV DIRECTOR" not in html or b"slot-model" not in html:
            raise RuntimeError(f"Unexpected root response: {r1.status}")
        if not is_companion_alive(port, timeout=3.0):
            raise RuntimeError("/ops/state check failed")

        conn_m = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn_m.request("GET", "/ops/models")
        rm = conn_m.getresponse()
        models_payload = json.loads(rm.read().decode("utf-8"))
        conn_m.close()
        if rm.status != 200 or "catalog" not in models_payload:
            raise RuntimeError(f"/ops/models check failed: {rm.status}")

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

    # 4. 24/7 Cloud / Mobile headless background server mode: python3 launcher.py --cloud
    if args and args[0] in ("--cloud", "--headless", "--remote"):
        os.environ["DIRECTOR_REMOTE_ACCESS"] = "1"
        from core.db_path import SOURCE_ROOT, BUNDLE_ROOT, ensure_runtime_files
        ensure_runtime_files(SOURCE_ROOT, BUNDLE_ROOT)
        import companion_server
        companion_server.run()
        return 0

    from core.db_path import SOURCE_ROOT, BUNDLE_ROOT, ensure_runtime_files
    root = ensure_runtime_files(SOURCE_ROOT, BUNDLE_ROOT)
    unblock_windows_files(root)
    _redirect_null_streams(root / "data")

    try:
        cfg = json.loads((root / "companion.json").read_text(encoding="utf-8"))
        port = int(cfg.get("port", 8081))
    except Exception:
        port = 8081
    url = f"http://127.0.0.1:{port}"
    profile_dir = root / "data" / "window_profile"

    # Single-instance check: if already running, simply open/focus the UI window and exit cleanly.
    if is_companion_alive(port):
        open_app_window(url)
        return 0

    pid_file = root / "data" / "companion.pid"
    try:
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass

    try:
        import companion_server
        server = companion_server.create_server()
        run_desktop_session(server, url=url, profile_dir=profile_dir)
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
