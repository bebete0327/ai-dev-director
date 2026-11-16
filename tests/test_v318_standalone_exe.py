"""Tests for standalone Windows executable (PyInstaller frozen mode & windowless launcher)."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import companion_server
import core.db_path as db_path
import core.env_utils as eu
from core.verifier import Verifier
import launcher


def test_frozen_roots_portable_vs_standalone(monkeypatch, tmp_path):
    portable = tmp_path / "portable"
    portable.mkdir()
    exe_portable = portable / "AI-Dev-Director.exe"
    exe_portable.write_text("", encoding="utf-8")
    (portable / "config.yaml").write_text("head:\n  provider: claude\n", encoding="utf-8")

    meipass = tmp_path / "meipass"
    meipass.mkdir()

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_portable))

    assert db_path.resolve_bundle_root() == meipass.resolve()
    assert db_path.resolve_source_root() == portable.resolve()

    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    exe_alone = desktop / "AI-Dev-Director.exe"
    exe_alone.write_text("", encoding="utf-8")
    appdata = tmp_path / "LocalAppData"
    monkeypatch.setattr(sys, "executable", str(exe_alone))
    monkeypatch.setenv("LOCALAPPDATA", str(appdata))

    assert db_path.resolve_source_root() == (appdata / "AI-Dev-Director").resolve()


def test_ensure_runtime_files_seeds_defaults(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "config.yaml").write_text("head:\n  provider: claude\n", encoding="utf-8")
    (bundle / "companion.json").write_text('{"port": 8081, "allowed_projects": []}\n', encoding="utf-8")

    runtime = tmp_path / "runtime"
    out = db_path.ensure_runtime_files(runtime, bundle)
    assert out == runtime.resolve()
    assert (runtime / "data").is_dir()
    assert (runtime / "config.yaml").exists()
    assert json.loads((runtime / "companion.json").read_text(encoding="utf-8"))["port"] == 8081


def test_windows_subprocess_kwargs_only_active_when_frozen_or_requested(monkeypatch):
    monkeypatch.setattr(eu.os, "name", "nt")
    monkeypatch.setattr(eu.sys, "frozen", False, raising=False)
    monkeypatch.delenv("DIRECTOR_NO_CONSOLE", raising=False)
    assert eu.windows_subprocess_kwargs() == {}

    monkeypatch.setattr(eu.sys, "frozen", True, raising=False)
    kw = eu.windows_subprocess_kwargs()
    assert "creationflags" in kw and kw["creationflags"] != 0


def test_verifier_does_not_reinvoke_frozen_exe_as_python(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cls, argv, **kwargs):
        seen["argv"] = list(argv)
        return SimpleNamespace(timed_out=False, exit_code=0, stdout="ok", stderr="")

    monkeypatch.setattr("core.verifier.ProcessTreeRunner.run", classmethod(fake_run))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "AI-Dev-Director.exe"))
    monkeypatch.setattr("core.verifier.shutil.which", lambda name: "/usr/bin/python3" if name in ("python", "python3") else None)

    res = Verifier._execute_cmd("python -m pytest tests/", tmp_path)
    assert res["passed"] is True
    assert seen["argv"][0] == "/usr/bin/python3"


def test_pick_folder_uses_self_flag_when_frozen(monkeypatch):
    seen = {}

    def fake_run(argv, capture_output, timeout):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=b'{"path": "C:/work/demo"}')

    monkeypatch.setattr(companion_server.sys, "frozen", True, raising=False)
    monkeypatch.setattr(companion_server.sys, "executable", "C:/app/AI-Dev-Director.exe")
    monkeypatch.setattr(companion_server.subprocess, "run", fake_run)

    assert companion_server.pick_folder("C:/work") == {"path": "C:/work/demo"}
    assert seen["argv"] == ["C:/app/AI-Dev-Director.exe", "--pick-folder", "C:/work"]


def test_launcher_smoke_test_and_single_instance(monkeypatch):
    assert launcher.run_smoke_test() == 0

    opened = []
    monkeypatch.setattr(launcher, "is_companion_alive", lambda port, timeout=1.5: True)
    monkeypatch.setattr(launcher, "open_app_window", lambda url: opened.append(url))
    assert launcher.main([]) == 0
    assert opened and opened[0].startswith("http://127.0.0.1:")
