"""v3.1.9 tests: per-provider CLI --model selection, /ops/models endpoint, window-close auto-shutdown, and MotW unblock."""
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import pytest

import launcher
from agents.claude_head import ClaudeHead
from agents.claude_reviewer import ClaudeReviewer
from agents.codex_head import CodexHead
from agents.codex_reviewer import CodexReviewer
from agents.antigravity_builder import AntigravityBuilder
from agents.google_roles import GoogleHead
from core import model_settings, role_settings
from core.orchestrator import Orchestrator
from core.process_runner import ProcessResult, ProcessTreeRunner
from core.research_runner import TriadResearchRunner
from schemas.order import OrderSchema
from tests.test_v301_companion_http import http_server, request  # noqa: F401


def test_model_settings_validation_and_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr(model_settings, "PATH", tmp_path / "model_settings.json")
    assert model_settings.load() == {"claude": "", "antigravity": "", "openai": ""}

    saved = model_settings.save({
        "claude": "opus",
        "antigravity": "Orchestrator (Argon)",
        "openai": "gpt-6-astra",
    })
    assert saved == {
        "claude": "opus",
        "antigravity": "Orchestrator (Argon)",
        "openai": "gpt-6-astra",
    }
    # Resetting one provider to 'default' clears it while keeping the others
    updated = model_settings.save({"claude": "default"})
    assert updated["claude"] == ""
    assert updated["antigravity"] == "Orchestrator (Argon)"

    # Reject CLI flag injection or control characters without corrupting file
    for bad in ("--dangerously-skip-permissions", "-m", "opus\n--bad", "bad;rm"):
        with pytest.raises(ValueError):
            model_settings.save({"openai": bad})
    assert model_settings.load() == updated


def test_orchestrator_applies_provider_models_and_keeps_failover_provider_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(role_settings, "PATH", tmp_path / "roles.json")
    monkeypatch.setattr(model_settings, "PATH", tmp_path / "models.json")
    role_settings.save({"head": "claude", "builder": "antigravity", "reviewer": "openai"})
    model_settings.save({"claude": "opus", "antigravity": "Gemini 4 Hydrogen", "openai": "gpt-6.1-sol"})

    cfg = model_settings.apply(role_settings.apply({}))
    orch = Orchestrator(cfg)
    assert orch.head.model == "opus"
    assert orch.builder.model == "Gemini 4 Hydrogen"
    assert orch.reviewer.model == "gpt-6.1-sol"

    # Failover override from Claude HEAD -> OpenAI HEAD must use OpenAI's model, NEVER Claude's 'opus'!
    failover_head = orch._init_head(provider_override="openai")
    assert failover_head.model == "gpt-6.1-sol"


def test_cli_commands_inject_model_flag_only_when_selected(tmp_path, monkeypatch):
    # 1. TriadResearchRunner (used by ProposalBuilder, GoogleHead/Reviewer, and TRIAD)
    r_default = TriadResearchRunner(executables={"OPENAI": "/bin/codex", "ANTHROPIC": "/bin/claude", "GOOGLE": "/bin/agy"})
    assert "--model" not in r_default._command("OPENAI", "/bin/codex", "hi")[0]
    assert "--model" not in r_default._command("ANTHROPIC", "/bin/claude", "hi")[0]
    assert "--model" not in r_default._command("GOOGLE", "/bin/agy", "hi")[0]

    r_custom = TriadResearchRunner(
        executables={"OPENAI": "/bin/codex", "ANTHROPIC": "/bin/claude", "GOOGLE": "/bin/agy"},
        models={"OPENAI": "gpt-6-astra", "ANTHROPIC": "claude-opus-5-5", "GOOGLE": "Orchestrator (Argon)"},
    )
    assert r_custom._command("OPENAI", "/bin/codex", "hi")[0] == [
        "/bin/codex", "exec", "--model", "gpt-6-astra", "--sandbox", "read-only", "-"
    ]
    assert r_custom._command("ANTHROPIC", "/bin/claude", "hi")[0] == [
        "/bin/claude", "--print", "--output-format", "json", "--model", "claude-opus-5-5"
    ]
    assert r_custom._command("GOOGLE", "/bin/agy", "hi")[0] == [
        "/bin/agy", "--model", "Orchestrator (Argon)", "--input-format", "stream-json", "--output-format", "stream-json", "--sandbox"
    ]

    # 2. Direct agent invocations (ClaudeHead, ClaudeReviewer, CodexHead, CodexReviewer)
    seen_argv = []

    def fake_run(argv, **kwargs):
        seen_argv.append(list(argv))
        inner = '{"type":"QUESTION","message":"hi","verdict":"pass","p0":[],"p1":[],"p2":[],"summary":"ok"}'
        return ProcessResult(
            exit_code=0,
            stdout=json.dumps({"result": inner,
                               "verdict": "pass", "p0": [], "p1": [], "p2": [], "summary": "ok",
                               "type": "QUESTION", "message": "hi"}),
            stderr="",
            timed_out=False,
            duration_sec=0.1,
            pid=999,
        )

    monkeypatch.setattr(ProcessTreeRunner, "run", fake_run)

    def last_exec_argv():
        cmds = [c for c in seen_argv if "--version" not in c]
        return cmds[-1]

    ClaudeHead(executable=sys.executable).chat("hello", {}, [])
    assert "--model" not in last_exec_argv()

    ClaudeHead(executable=sys.executable, model="sonnet").chat("hello", {}, [])
    assert ["--model", "sonnet"] == last_exec_argv()[-2:]

    ClaudeReviewer(executable=sys.executable, model="haiku").review(
        OrderSchema(goal="g", allowed_paths=["src/**"]), "diff", {"passed": True}, worktree_path=str(tmp_path)
    )
    assert ["--model", "haiku"] == last_exec_argv()[-2:]

    CodexHead(executable=sys.executable, model="gpt-6.1-sol").chat("hello", {}, [])
    assert last_exec_argv()[1:4] == ["exec", "--model", "gpt-6.1-sol"]

    CodexReviewer(executable=sys.executable, model="gpt-6-luna").review(
        OrderSchema(goal="g", allowed_paths=["src/**"]), "diff", {"passed": True}, worktree_path=str(tmp_path)
    )
    assert last_exec_argv()[1:4] == ["exec", "--model", "gpt-6-luna"]

    # GoogleHead forwards model into TriadResearchRunner
    gh_models = []
    monkeypatch.setattr(
        TriadResearchRunner,
        "invoke",
        lambda self, dom, prompt, cwd, stage: (gh_models.append(self.models.get(dom)) or {
            "output": '{"type":"QUESTION","message":"ok"}',
            "invocation": "agy",
            "exit_code": 0,
            "duration_sec": 0.1,
        }),
    )
    GoogleHead(executable=sys.executable, model="Gemini 4 Argon").chat("hello", {}, [])
    assert gh_models == ["Gemini 4 Argon"]

    # AntigravityBuilder provenance carries custom model
    ab = AntigravityBuilder(executable=sys.executable, model="Gemini 4 Hydrogen")
    assert ab.get_provenance().model == "Gemini 4 Hydrogen"


def test_ops_models_http_endpoint_and_roles_payload(http_server, tmp_path, monkeypatch):
    monkeypatch.setattr(model_settings, "PATH", tmp_path / "models.json")
    code, body = request(http_server, "GET", "/ops/roles")
    assert code == 200
    roles_data = json.loads(body)
    assert "roles" in roles_data and "models" in roles_data and "model_catalog" in roles_data

    # Unauthenticated POST must be rejected
    assert request(http_server, "POST", "/ops/models", json.dumps({"claude": "opus"}).encode(),
                   {"Content-Type": "application/json"})[0] == 403

    # Invalid model flag injection rejected with 400
    code_bad, _ = request(
        http_server,
        "POST",
        "/ops/models",
        json.dumps({"claude": "--bad-flag"}).encode(),
        {"Content-Type": "application/json", "X-Solomon-Bridge": "test-token"},
    )
    assert code_bad == 400

    # Valid model update saved and returned
    code_ok, out = request(
        http_server,
        "POST",
        "/ops/models",
        json.dumps({"claude": "opus", "openai": "gpt-6-astra"}).encode(),
        {"Content-Type": "application/json", "X-Solomon-Bridge": "test-token"},
    )
    assert code_ok == 200
    saved = json.loads(out)["models"]
    assert saved["claude"] == "opus" and saved["openai"] == "gpt-6-astra"


def test_window_close_automatically_shuts_down_server_and_kills_active_clis(tmp_path, monkeypatch):
    import companion_server
    import dashboard_server as d

    monkeypatch.setattr(launcher, "is_companion_alive", lambda port, timeout=1.0: True)
    server = d.DashboardServer(("127.0.0.1", 0), companion_server.CompanionHandler)
    server.bridge_token = "tok"
    server.stopping = False
    server.had_client_contact = False
    server.last_client_contact = time.monotonic()
    server.bridge = SimpleNamespace(submit=lambda p: {}, list=lambda: [], allowed=set())

    killed_calls = []
    monkeypatch.setattr(ProcessTreeRunner, "kill_active", classmethod(lambda cls: killed_calls.append(True) or []))

    class FakeWindowProc:
        pid = 42424

        def __init__(self):
            self.closed = False

        def poll(self):
            return 0 if self.closed else None

    fake_proc = FakeWindowProc()
    monkeypatch.setattr(launcher, "open_app_window", lambda url, profile_dir=None: fake_proc)

    def close_window_soon():
        time.sleep(0.12)
        fake_proc.closed = True

    threading.Thread(target=close_window_soon, daemon=True).start()
    launcher.run_desktop_session(
        server,
        url=f"http://127.0.0.1:{server.server_address[1]}",
        profile_dir=tmp_path / "win_profile",
        startup_handoff_sec=0.04,
        poll_interval=0.02,
    )
    assert server.stopping is True
    assert len(killed_calls) >= 1


def test_system_stop_closes_dedicated_window_process(tmp_path, monkeypatch):
    import companion_server
    import dashboard_server as d

    monkeypatch.setattr(launcher, "is_companion_alive", lambda port, timeout=1.0: True)
    server = d.DashboardServer(("127.0.0.1", 0), companion_server.CompanionHandler)
    server.bridge_token = "tok"
    server.stopping = False
    server.had_client_contact = False
    server.last_client_contact = time.monotonic()
    server.bridge = SimpleNamespace(submit=lambda p: {}, list=lambda: [], allowed=set())

    terminated_pids = []
    monkeypatch.setattr(ProcessTreeRunner, "kill_process_tree", staticmethod(lambda pid: terminated_pids.append(pid) or True))

    class FakeWindowProc:
        pid = 51515

        def poll(self):
            return 0 if 51515 in terminated_pids else None

    monkeypatch.setattr(launcher, "open_app_window", lambda url, profile_dir=None: FakeWindowProc())

    def trigger_ui_shutdown():
        time.sleep(0.10)
        server.stopping = True
        server.shutdown()

    threading.Thread(target=trigger_ui_shutdown, daemon=True).start()
    launcher.run_desktop_session(
        server,
        url=f"http://127.0.0.1:{server.server_address[1]}",
        profile_dir=tmp_path / "win_profile",
        startup_handoff_sec=0.04,
        poll_interval=0.02,
    )
    assert 51515 in terminated_pids


def test_windows_motw_zone_identifier_self_unblock(tmp_path, monkeypatch):
    exe_file = tmp_path / "AI-Dev-Director.exe"
    exe_file.write_bytes(b"MZ")
    deleted_streams = []

    monkeypatch.setattr(launcher, "_IS_WINDOWS", True)
    monkeypatch.setattr(launcher.sys, "frozen", True, raising=False)
    monkeypatch.setattr(launcher.sys, "executable", str(exe_file))
    monkeypatch.setattr(launcher.os, "remove", lambda p: deleted_streams.append(p))

    removed = launcher.unblock_windows_files(tmp_path)
    assert removed >= 1
    assert f"{exe_file}:Zone.Identifier" in deleted_streams
