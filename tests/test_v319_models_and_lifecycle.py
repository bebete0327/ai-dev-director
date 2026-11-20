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
        "antigravity": "gemini-pro-orchestrator-g1-pro",
        "openai": "gpt-6-astra",
    }
    # Resetting one provider to 'default' clears it while keeping the others
    updated = model_settings.save({"claude": "default"})
    assert updated["claude"] == ""
    assert updated["antigravity"] == "gemini-pro-orchestrator-g1-pro"

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
    assert orch.builder.model == "gemini-4-hydrogen"
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
        "/bin/agy", "--model", "gemini-pro-orchestrator-g1-pro", "--input-format", "stream-json", "--output-format", "stream-json", "--sandbox"
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

    # GoogleHead forwards canonicalized model into TriadResearchRunner
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
    assert gh_models == ["gemini-4-argon"]

    # AntigravityBuilder provenance carries custom model
    ab = AntigravityBuilder(executable=sys.executable, model="gemini-4-hydrogen")
    assert ab.get_provenance().model == "gemini-4-hydrogen"


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


def test_reasoning_effort_settings_and_cli_flags(tmp_path, monkeypatch):
    from core import quota_tracker
    monkeypatch.setattr(model_settings, "PATH", tmp_path / "models.json")

    model_settings.save({"claude": "opus", "openai": "gpt-6-astra"})
    eff = model_settings.save_efforts({"claude": "max", "openai": "xhigh", "antigravity": "high"})
    assert eff == {"claude": "max", "antigravity": "high", "openai": "xhigh"}
    # Saving models later must preserve saved efforts
    model_settings.save({"antigravity": "Gemini 4 Argon"})
    assert model_settings.load_efforts() == eff

    with pytest.raises(ValueError):
        model_settings.save_efforts({"claude": "minimal"})

    r = TriadResearchRunner(
        executables={"OPENAI": "/bin/codex", "ANTHROPIC": "/bin/claude", "GOOGLE": "/bin/agy"},
        models={"OPENAI": "gpt-6-astra", "ANTHROPIC": "opus", "GOOGLE": "Gemini 4 Argon"},
        efforts={"OPENAI": "xhigh", "ANTHROPIC": "max", "GOOGLE": "high"},
    )
    assert r._command("OPENAI", "/bin/codex", "hi")[0] == [
        "/bin/codex", "exec", "--model", "gpt-6-astra", "-c", "model_reasoning_effort=xhigh", "--sandbox", "read-only", "-"
    ]
    assert r._command("ANTHROPIC", "/bin/claude", "hi")[0] == [
        "/bin/claude", "--print", "--output-format", "json", "--model", "opus", "--effort", "max"
    ]
    assert r._command("GOOGLE", "/bin/agy", "hi")[0] == [
        "/bin/agy", "--model", "gemini-4-argon", "--effort", "high", "--input-format", "stream-json", "--output-format", "stream-json", "--sandbox"
    ]

    # Codex & Claude local quota snapshot parsing (active vs expired resets_at)
    codex_dir = tmp_path / "codex_sessions"
    codex_dir.mkdir()
    (codex_dir / "rollout-1.jsonl").write_text(
        json.dumps({"payload": {"info": {"rate_limits": {"primary": {"used_percent": 32.5, "resets_at": 2000000000}, "secondary": {"used_percent": 11.0, "resets_at": 1500000000}}}}}) + "\n",
        encoding="utf-8",
    )
    claude_dir = tmp_path / "claude_projects" / "proj"
    claude_dir.mkdir(parents=True)
    (claude_dir / "sess.jsonl").write_text(
        json.dumps({"uuid": "u1", "message": {"id": "m1", "model": "claude-opus-5-5", "usage": {"input_tokens": 1200, "output_tokens": 300}}}) + "\n",
        encoding="utf-8",
    )
    snap = quota_tracker.snapshot(codex_dir=codex_dir, claude_dir=tmp_path / "claude_projects", now_ts=1700000000.0)
    assert snap["providers"]["openai"]["rate_limits"]["primary_percent"] == 32.5
    assert snap["providers"]["openai"]["rate_limits"]["primary_expired"] is False
    assert snap["providers"]["openai"]["rate_limits"]["secondary_percent"] == 0.0
    assert snap["providers"]["openai"]["rate_limits"]["secondary_expired"] is True
    assert snap["providers"]["claude"]["today_calls"] == 1
    assert snap["providers"]["claude"]["today_tokens"] == 1500
    assert any(m["model"] == "claude-opus-5-5" and m["tokens"] == 1500 for m in snap["providers"]["claude"]["by_model"])


def test_http_usage_limits_and_effort_post(http_server, tmp_path, monkeypatch):
    monkeypatch.setattr(model_settings, "PATH", tmp_path / "models.json")
    code, body = request(http_server, "GET", "/ops/usage-limits")
    assert code == 200
    snap = json.loads(body)
    assert "providers" in snap and {"claude", "openai", "antigravity"} <= set(snap["providers"])

    code_post, post_body = request(
        http_server,
        "POST",
        "/ops/models",
        json.dumps({"efforts": {"claude": "high", "openai": "xhigh"}}).encode(),
        {"Content-Type": "application/json", "X-Solomon-Bridge": http_server.bridge_token},
    )
    assert code_post == 200
    out = json.loads(post_body)
    assert out["efforts"]["claude"] == "high"
    assert out["efforts"]["openai"] == "xhigh"


def test_claude_three_bars_codex_unified_weekly_and_antigravity_four_bars(tmp_path):
    import base64
    import sqlite3
    from core import quota_tracker

    now_ts = 1763305260.0  # Fixed reference timestamp

    # 1. Claude: 3 bars (5시간 제한, 주간 · 모든 모델, 주간 · Fable) + reset times
    claude_payload = {
        "five_hour": {"utilization": 100.0, "resets_at": now_ts + 7200},
        "seven_day": {"utilization": 54.0, "resets_at": now_ts + 172800},
        "seven_day_sonnet": {"utilization": 100.0, "resets_at": now_ts + 172800},
        "seven_day_opus": None,
    }
    c_parsed = quota_tracker._parse_claude_oauth_payload(claude_payload, now_ts=now_ts)
    assert c_parsed is not None
    c_wins = c_parsed["windows"]
    assert [w["label"] for w in c_wins] == ["5시간 제한", "주간 · 모든 모델", "주간 · Fable"]
    assert [w["used_pct"] for w in c_wins] == [100.0, 54.0, 100.0]
    assert all("초기화" in w["right_text"] for w in c_wins)
    assert c_wins[0]["state"] == "exhausted"
    assert c_wins[1]["state"] == "ok"
    assert c_wins[2]["state"] == "exhausted"

    # 2. Codex: unified weekly limit from /wham/usage (0% 남음, 1일 23시간 후 초기화)
    wham_payload = {
        "plan_type": "plus",
        "rate_limit": {
            "allowed": False,
            "limit_reached": True,
            "primary_window": {
                "used_percent": 100,
                "limit_window_seconds": 604800,
                "reset_after_seconds": 170400,
                "reset_at": int(now_ts + 170400),
            },
            "secondary_window": None,
        },
    }
    x_parsed = quota_tracker._parse_codex_wham_payload(wham_payload, now_ts=now_ts)
    assert x_parsed is not None
    x_wins = x_parsed["windows"]
    assert len(x_wins) == 1
    assert x_wins[0]["label"] == "주간 사용 한도"
    assert x_wins[0]["used_pct"] == 100.0
    assert x_wins[0]["remaining_pct"] == 0.0
    assert "1일 23시간 후 초기화" in x_wins[0]["right_text"]
    assert "0% 남음" in x_wins[0]["right_text"]
    assert x_wins[0]["state"] == "exhausted"

    # 3. Antigravity: 4 bars (Gemini Weekly/5h + Claude/GPT Weekly/5h) + state.vscdb nested base64 token extraction
    vscdb = tmp_path / "state.vscdb"
    fake_tok = "ya29.a0AfB_byCdefghijklmnopqrstuvwxyz1234567890"
    inner_b64 = base64.b64encode(f"proto_prefix:{fake_tok}:suffix".encode("utf-8")).decode("ascii")
    outer_b64 = base64.b64encode(f"wrap:{inner_b64}".encode("utf-8")).decode("ascii")
    with sqlite3.connect(str(vscdb)) as conn:
        conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
            ("antigravityUnifiedStateSync.oauthToken", outer_b64),
        )
    extracted = quota_tracker._extract_agy_oauth_token(vscdb)
    assert extracted == fake_tok

    agy_payload = {
        "groups": [
            {
                "displayName": "Gemini Models",
                "buckets": [
                    {
                        "bucketId": "gemini-weekly",
                        "displayName": "Weekly Limit Remaining",
                        "remaining": {"remainingFraction": 0.45},
                        "description": "You have used some of your weekly limit, it will fully refresh in 4 days, 9 hours.",
                    },
                    {
                        "bucketId": "gemini-5h",
                        "displayName": "Five Hour Limit Remaining",
                        "remaining": {"remainingFraction": 0.81},
                        "description": "You have used some of your five hour limit, it will fully refresh in 2 hours, 37 minutes.",
                    },
                ],
            },
            {
                "displayName": "Claude and GPT models",
                "buckets": [
                    {
                        "bucketId": "3p-weekly",
                        "displayName": "Weekly Limit Remaining",
                        "remaining": {"remainingFraction": 0.51},
                        "description": "You have used some of your weekly limit, it will fully refresh in 4 days, 23 hours.",
                    },
                    {
                        "bucketId": "3p-5h",
                        "displayName": "Five Hour Limit Remaining",
                        "remaining": {"remainingFraction": 1.0},
                        "description": "You have not used any of your five hour limit.",
                    },
                ],
            },
        ]
    }
    a_parsed = quota_tracker._parse_antigravity_quota_payload(agy_payload, now_ts=now_ts)
    assert a_parsed is not None
    a_wins = a_parsed["windows"]
    assert [w["label"] for w in a_wins] == [
        "Gemini · 주간 한도",
        "Gemini · 5시간 한도",
        "Claude/GPT · 주간 한도",
        "Claude/GPT · 5시간 한도",
    ]
    assert [w["remaining_pct"] for w in a_wins] == [45.0, 81.0, 51.0, 100.0]
    assert a_wins[0]["right_text"] == "4일 9시간 후 초기화 · 45% 남음"
    assert a_wins[1]["right_text"] == "2시간 37분 후 초기화 · 81% 남음"
    assert a_wins[2]["right_text"] == "4일 23시간 후 초기화 · 51% 남음"
    assert a_wins[3]["right_text"] == "미사용 · 100% 남음"


def test_cloud_remote_access_and_auth_sync_bundle(monkeypatch):
    import dashboard_server as d

    # Default desktop mode: LAN/cloud Host headers are rejected
    monkeypatch.delenv("DIRECTOR_REMOTE_ACCESS", raising=False)
    assert d.is_request_origin_allowed({"Host": "100.64.1.25:8081"}, 8081)[0] is False

    # Cloud/Mobile mode enabled: Tailscale / Cloud VM / Mobile Host headers are allowed
    monkeypatch.setenv("DIRECTOR_REMOTE_ACCESS", "1")
    assert d.is_request_origin_allowed({"Host": "100.64.1.25:8081"}, 8081)[0] is True
    assert d.is_request_origin_allowed(
        {"Host": "100.64.1.25:8081", "Origin": "http://100.64.1.25:8081"}, 8081
    )[0] is True
    assert d.is_request_origin_allowed(
        {"Host": "100.64.1.25:8081", "Origin": "http://evil.example.com"}, 8081
    )[0] is False


def test_live_model_discovery_and_custom_model_persistence(http_server, tmp_path, monkeypatch):
    monkeypatch.setattr(model_settings, "PATH", tmp_path / "models.json")

    # 1. Built-in presets include Claude Fable 5.2 and GPT-6.2 Astra
    base_cat = model_settings.catalog()
    claude_ids = [m["id"] for m in base_cat["claude"]]
    openai_ids = [m["id"] for m in base_cat["openai"]]
    assert "claude-fable-5-2" in claude_ids
    assert "gpt-6.2-astra" in openai_ids

    # 2. Live discovery reads Codex models_cache.json and Claude session/settings models
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    (codex_home / "models_cache.json").write_text(
        json.dumps({"models": [{"slug": "gpt-6.5-nova", "display_name": "GPT-6.5 Nova", "description": "신규 출시 모델"}]}),
        encoding="utf-8",
    )
    claude_home = tmp_path / "claude_home"
    claude_home.mkdir()
    (claude_home / "settings.json").write_text(json.dumps({"model": "claude-fable-5-3"}), encoding="utf-8")

    refreshed_cat = model_settings.catalog(refresh=True, codex_home=codex_home, claude_home=claude_home)
    assert any(m["id"] == "gpt-6.5-nova" and m["label"] == "GPT-6.5 Nova" for m in refreshed_cat["openai"])
    assert any(m["id"] == "claude-fable-5-3" for m in refreshed_cat["claude"])

    # 3. Custom model saved by user is permanently added to catalog
    model_settings.save({"openai": "gpt-7-omega"})
    after_save_cat = model_settings.catalog()
    assert any(m["id"] == "gpt-7-omega" for m in after_save_cat["openai"])

    # 4. HTTP endpoint /ops/models?refresh=1 returns catalog and mobile_urls
    code, body = request(http_server, "GET", "/ops/models?refresh=1")
    assert code == 200
    payload = json.loads(body)
    assert "catalog" in payload and "mobile_urls" in payload


def test_agy_protobuf_offset_and_pool_dedup_and_tunnel_state(http_server):
    import base64
    from core import quota_tracker

    # 1. Protobuf-prefixed inner base64 (1..3 ASCII tag/length bytes shifting 4-byte alignment)
    raw_token = "ya29.a0AfH6SMB_real_antigravity_oauth_token_1234567890"
    inner_b64 = base64.b64encode(f"\x0a\x37{raw_token}".encode("utf-8")).decode("ascii")
    # Prepend 2 ASCII protobuf tag/length characters ('\x12Z' -> 'Z' is base64 char, shifting alignment by 1)
    outer_proto = b"\x0a\x0eoauthTokenInfo\x12Z" + inner_b64.encode("ascii")
    outer_b64 = base64.b64encode(outer_proto).decode("ascii")
    assert quota_tracker._search_ya29_in_text(outer_b64) == raw_token

    # 2. GetUserStatus fallback maps Orchestrator (Argon) to Gemini and Claude Fable 5.2 to Claude/GPT (no duplicate '모델 · 한도')
    status_payload = {
        "userStatus": {
            "cascadeModelConfigData": {
                "clientModelConfigs": [
                    {"label": "Gemini 4 Carbon", "quotaInfo": {"remainingFraction": 0.147, "resetTime": "2026-11-18T16:30:00Z"}},
                    {"label": "Orchestrator (Argon)", "quotaInfo": {"remainingFraction": 0.147, "resetTime": "2026-11-18T16:30:00Z"}},
                    {"label": "Claude Fable 5.2", "quotaInfo": {"remainingFraction": 1.0, "resetTime": "2026-11-18T20:00:00Z"}},
                ]
            }
        }
    }
    parsed = quota_tracker._parse_antigravity_quota_payload(status_payload, now_ts=1795015000.0)
    assert parsed is not None
    labels = [w["label"] for w in parsed["windows"]]
    assert labels == ["Gemini · 한도", "Claude/GPT · 한도"]

    # 3. /ops/state and /ops/usage-limits include mobile_urls and tunnel metadata
    import dashboard_server as d
    d._ensure_schema()
    code, body = request(http_server, "GET", "/ops/state")
    assert code == 200
    state = json.loads(body)
    assert "mobile_urls" in state and "tunnel" in state
    code2, body2 = request(http_server, "GET", "/ops/usage-limits")
    assert code2 == 200
    ul = json.loads(body2)
    assert "mobile_urls" in ul and "tunnel" in ul


def test_expanded_timeouts_and_antigravity_slug_canonicalization_and_cli_default_fallback(tmp_path, monkeypatch):
    import subprocess
    import yaml
    from core import db_path

    # 1. config.yaml and Orchestrator default timeouts are expanded (builder 2400s, head/reviewer 900s, no_progress 600s)
    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["timeouts"]["builder"] == 2400
    assert cfg["timeouts"]["head"] == 900
    assert cfg["timeouts"]["reviewer"] == 900
    assert cfg["timeouts"]["no_progress"] == 600

    orch_default = Orchestrator({})
    assert orch_default.builder.timeout == 2400
    assert orch_default.builder.no_progress_timeout == 600
    assert orch_default.head.timeout == 900
    assert orch_default.reviewer.timeout == 900

    # 2. Legacy untouched runtime config.yaml is automatically upgraded by ensure_runtime_files()
    rt_dir = tmp_path / "runtime_root"
    rt_dir.mkdir()
    (rt_dir / "config.yaml").write_text(
        "timeouts:\n  head: 300\n  builder: 600\n  reviewer: 300\n  no_progress: 180\n  heartbeat: 30\n",
        encoding="utf-8",
    )
    db_path.ensure_runtime_files(root=rt_dir, bundle=Path(__file__).resolve().parent.parent)
    migrated = yaml.safe_load((rt_dir / "config.yaml").read_text(encoding="utf-8"))
    assert migrated["timeouts"]["builder"] == 2400 and migrated["timeouts"]["no_progress"] == 600

    # 3. Existing saved "Gemini 4 Carbon" display label in model_settings.json canonicalizes to "gemini-4-carbon",
    #    legacy "max" effort maps to "xhigh", and Orchestrator omits --effort.
    models_file = tmp_path / "legacy_models.json"
    models_file.write_text(
        json.dumps({"antigravity": "Gemini 4 Carbon", "_efforts": {"antigravity": "max"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(model_settings, "PATH", models_file)
    assert model_settings.load()["antigravity"] == "gemini-4-carbon"
    assert model_settings.load_efforts()["antigravity"] == "xhigh"

    r_orch = TriadResearchRunner(
        executables={"GOOGLE": "/bin/agy"},
        models={"GOOGLE": "Orchestrator (Carbon)"},
        efforts={"GOOGLE": "high"},
    )
    assert r_orch._command("GOOGLE", "/bin/agy", "hi")[0] == [
        "/bin/agy", "--model", "gemini-pro-orchestrator", "--input-format", "stream-json", "--output-format", "stream-json", "--sandbox"
    ]

    # 4. If Antigravity rejects a tier-restricted custom model (e.g. gemini-4-carbon on non-Ultra -> exit 1),
    #    TriadResearchRunner automatically retries with CLI account default (no --model / --effort) instead of
    #    failing over to Claude Builder!
    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "init"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=ws, check=True, capture_output=True)

    seen_cmds = []

    def fake_agy_run(argv, **kwargs):
        if "--version" in argv:
            return ProcessResult(exit_code=0, stdout="agy 1.0", stderr="", timed_out=False, duration_sec=0.05, pid=1)
        seen_cmds.append(list(argv))
        if "--model" in argv:
            return ProcessResult(
                exit_code=1,
                stdout="",
                stderr="INVALID_ARGUMENT: model gemini-4-carbon is not available for your subscription tier",
                timed_out=False,
                duration_sec=0.3,
                pid=1,
            )
        stream_ok = json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": '{"files":[],"summary":"ok"}'}})+ "\n"
        return ProcessResult(exit_code=0, stdout=stream_ok, stderr="", timed_out=False, duration_sec=0.4, pid=2)

    monkeypatch.setattr(ProcessTreeRunner, "run", staticmethod(fake_agy_run))
    runner = TriadResearchRunner(
        executables={"GOOGLE": sys.executable},
        models={"GOOGLE": "Gemini 4 Carbon"},
        efforts={"GOOGLE": "high"},
    )
    res = runner.invoke("GOOGLE", "build step", str(ws), "BUILDING")
    assert res["attempt_number"] == 2
    assert len(seen_cmds) == 2
    assert ["--model", "gemini-4-carbon", "--effort", "high"] == seen_cmds[0][1:5]
    assert "--model" not in seen_cmds[1] and "--effort" not in seen_cmds[1]
    assert res["output"] == '{"files":[],"summary":"ok"}'


def test_v320_agy_slug_catalog_build_id_and_zero_pass_continuation(http_server, tmp_path, monkeypatch):
    import dashboard_server as d
    from core import usage
    from core.mission import MissionStore, latest_deliverable
    from core.verifier import Verifier

    d._ensure_schema()

    # 1. /ops/state returns build_id, and launcher.is_companion_alive stops a server with mismatched build_id
    code, body = request(http_server, "GET", "/ops/state")
    assert code == 200
    state = json.loads(body)
    assert state.get("build_id") == launcher.get_build_id()
    port = http_server.server_address[1]
    assert launcher.is_companion_alive(port, expected_build_id=state["build_id"]) is True

    stopped_ports = []
    monkeypatch.setattr(launcher, "_stop_outdated_companion", lambda p, csrf_token="", timeout=1.5: stopped_ports.append((p, csrf_token)))
    assert launcher.is_companion_alive(port, expected_build_id="old-stale-build-0000") is False
    assert stopped_ports == [(port, "test-token")]

    # 2. resolve_agy_cli_args maps bare names to `agy models` suffixed slugs, clamps Carbon xhigh->high,
    #    and drops Ultra-only Carbon to '' (CLI default) when the signed-in Pro account's `agy models` lacks it!
    pro_catalog = [
        "gemini-4-neon-xhigh",
        "gemini-4-neon-high",
        "gemini-4-neon-medium",
        "gemini-4-hydrogen-high",
        "gemini-3.8-flash-medium",
        "gemini-pro-orchestrator-neon",
        "gemini-pro-orchestrator-argon",
    ]
    assert model_settings.resolve_agy_cli_args("Gemini 4 Neon", "high", available_slugs=pro_catalog) == ("gemini-4-neon-high", "")
    assert model_settings.resolve_agy_cli_args("Gemini 4 Carbon", "xhigh", available_slugs=pro_catalog) == ("", "")
    assert model_settings.resolve_agy_cli_args("Orchestrator (Neon)", "high", available_slugs=pro_catalog) == ("gemini-pro-orchestrator-neon", "")

    ultra_catalog = pro_catalog + ["gemini-4-carbon-high", "gemini-pro-orchestrator"]
    assert model_settings.resolve_agy_cli_args("Gemini 4 Carbon", "xhigh", available_slugs=ultra_catalog) == ("gemini-4-carbon-high", "")

    # 3. Verifier automatically discovers root test_*.py (via pytest or unittest) when `tests/` folder does not exist
    proj_no_tests_dir = tmp_path / "chef_hunter"
    proj_no_tests_dir.mkdir()
    (proj_no_tests_dir / "test_locks_unittest.py").write_text(
        "import unittest\nclass LockTests(unittest.TestCase):\n    def test_lock_ok(self):\n        self.assertEqual(2 + 2, 4)\nif __name__ == '__main__':\n    unittest.main()\n",
        encoding="utf-8",
    )
    ver = Verifier.verify(str(proj_no_tests_dir), test_cmd="python -m pytest tests/", git_checks=False)
    assert ver["passed"] is True
    assert ver["test_counts"]["passed"] >= 1

    # 4. ai_usage records error_detail and surfaces it in recent_calls
    rid = usage.call_started(["agy.exe", "--model", "gemini-4-carbon"], "build prompt")
    usage.call_finished(
        rid,
        ["agy.exe", "--model", "gemini-4-carbon"],
        "build prompt",
        ProcessResult(exit_code=1, stdout="", stderr="INVALID_ARGUMENT: unknown model gemini-4-carbon", timed_out=False, duration_sec=6.2, pid=42),
    )
    rec = usage.recent_calls(5)[0]
    assert rec["status"] == "ERROR"
    assert "unknown model gemini-4-carbon" in rec["error"]

    # 5. latest_deliverable preserves the last attempted worktree when a mission stopped with 0/6 passed steps
    m_store = MissionStore(tmp_path / "missions_store")
    wt6 = tmp_path / "wt_step6"
    wt6.mkdir()
    proj_target = tmp_path / "target_proj"
    proj_target.mkdir()
    m_store.save({
        "mission_id": "MSN-261119-ZEROPASS",
        "project_path": str(proj_target),
        "goal": "locks",
        "status": "INCOMPLETE",
        "updated_at": "2026-11-19T20:30:00",
        "base_worktree": None,
        "steps": [
            {"index": 6, "order": {"goal": "step6"}, "run_id": "RUN-6", "result": {"final_state": "FAIL", "worktree_path": str(wt6)}}
        ],
    })
    assert latest_deliverable(str(proj_target), m_store) == str(wt6)


