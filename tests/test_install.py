"""Tests for the install/uninstall/doctor CLI, and the skill pack."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from extrovert import doctor, install
from extrovert.__main__ import build_parser, main

REPO_ROOT = Path(__file__).parent.parent
SKILL_MD = REPO_ROOT / "skills" / "extrovert" / "SKILL.md"
INSTALL_MD = REPO_ROOT / "INSTALL.md"

FAKE_LAUNCH = install.LaunchCommand(["/usr/local/bin/extrovert"])


def fake_which(mapping: dict[str, str | None]):
    return lambda name: mapping.get(name)


def fake_runner_factory(record: list[list[str]], list_output: str = ""):
    def runner(cmd, **kwargs):
        record.append(list(cmd))
        out = subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["claude", "mcp"] and "list" in cmd:
            out.stdout = list_output
        return out

    return runner


# -- launch command detection ---------------------------------------------------


def test_launch_command_prefers_installed_script() -> None:
    launch = install.detect_launch_command(which=fake_which({"extrovert": "/usr/local/bin/extrovert"}))
    assert launch.argv == ["/usr/local/bin/extrovert"]
    assert launch.to_json_entry() == {"command": "/usr/local/bin/extrovert"}


def test_launch_command_falls_back_to_python_module() -> None:
    launch = install.detect_launch_command(which=fake_which({}))
    assert launch.argv == [sys.executable, "-m", "extrovert"]
    assert launch.to_json_entry() == {"command": sys.executable, "args": ["-m", "extrovert"]}


# -- client detection -------------------------------------------------------------


def test_detect_nothing(tmp_path: Path) -> None:
    targets = install.detect_clients(home=tmp_path, which=fake_which({}))
    assert targets == []


def test_detect_claude_only(tmp_path: Path) -> None:
    targets = install.detect_clients(home=tmp_path, which=fake_which({"claude": "/opt/claude"}))
    assert [t.kind for t in targets] == ["claude-cli"]


def test_detect_kimi_mcp_json(tmp_path: Path) -> None:
    kimi = tmp_path / ".kimi-code"
    kimi.mkdir()
    (kimi / "mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    targets = install.detect_clients(home=tmp_path, which=fake_which({}))
    assert [t.kind for t in targets] == ["kimi-json"]
    assert targets[0].config_path == kimi / "mcp.json"


def test_detect_kimi_broken_json_is_manual(tmp_path: Path) -> None:
    kimi = tmp_path / ".kimi-code"
    kimi.mkdir()
    (kimi / "mcp.json").write_text("not json", encoding="utf-8")
    targets = install.detect_clients(home=tmp_path, which=fake_which({}))
    assert [t.kind for t in targets] == ["kimi-manual"]


def test_detect_kimi_toml_with_mcp_servers(tmp_path: Path) -> None:
    kimi = tmp_path / ".kimi-code"
    kimi.mkdir()
    (kimi / "config.toml").write_text('[mcp_servers.context7]\ncommand = "npx"\n', encoding="utf-8")
    targets = install.detect_clients(home=tmp_path, which=fake_which({}))
    assert [t.kind for t in targets] == ["kimi-toml"]


def test_detect_kimi_toml_without_mcp_servers_is_manual(tmp_path: Path) -> None:
    kimi = tmp_path / ".kimi-code"
    kimi.mkdir()
    (kimi / "config.toml").write_text('[providers.x]\nfoo = "bar"\n', encoding="utf-8")
    targets = install.detect_clients(home=tmp_path, which=fake_which({}))
    assert [t.kind for t in targets] == ["kimi-manual"]


def test_detect_kimi_dir_without_config(tmp_path: Path) -> None:
    (tmp_path / ".kimi-code").mkdir()
    targets = install.detect_clients(home=tmp_path, which=fake_which({}))
    assert [t.kind for t in targets] == ["kimi-manual"]


# -- kimi-json install / uninstall -----------------------------------------------


def make_kimi_json(tmp_path: Path) -> install.ClientTarget:
    kimi = tmp_path / ".kimi-code"
    kimi.mkdir()
    path = kimi / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "npx"}}}), encoding="utf-8")
    return install.ClientTarget("kimi-json", "Kimi Code (mcp.json)", config_path=path)


def test_install_kimi_json_adds_entry_and_backup(tmp_path: Path) -> None:
    target = make_kimi_json(tmp_path)
    result = install.install_kimi_json(target, FAKE_LAUNCH, dry_run=False)
    assert result.ok and result.changed
    data = json.loads(target.config_path.read_text())
    assert data["mcpServers"]["extrovert"] == {"command": "/usr/local/bin/extrovert"}
    assert data["mcpServers"]["other"] == {"command": "npx"}  # existing entries kept
    backup = target.config_path.with_name("mcp.json.bak")
    assert backup.exists()
    assert "extrovert" not in backup.read_text()

    second = install.install_kimi_json(target, FAKE_LAUNCH, dry_run=False)
    assert second.ok and not second.changed  # idempotent


def test_uninstall_kimi_json(tmp_path: Path) -> None:
    target = make_kimi_json(tmp_path)
    install.install_kimi_json(target, FAKE_LAUNCH, dry_run=False)
    result = install.uninstall_kimi_json(target, dry_run=False)
    assert result.ok and result.changed
    data = json.loads(target.config_path.read_text())
    assert "extrovert" not in data["mcpServers"]
    assert "other" in data["mcpServers"]


def test_kimi_json_dry_run_changes_nothing(tmp_path: Path) -> None:
    target = make_kimi_json(tmp_path)
    before = target.config_path.read_text()
    result = install.install_kimi_json(target, FAKE_LAUNCH, dry_run=True)
    assert result.ok and not result.changed
    assert target.config_path.read_text() == before
    assert not target.config_path.with_name("mcp.json.bak").exists()


# -- kimi-toml install / uninstall -----------------------------------------------


def make_kimi_toml(tmp_path: Path) -> install.ClientTarget:
    kimi = tmp_path / ".kimi-code"
    kimi.mkdir()
    path = kimi / "config.toml"
    path.write_text('[providers.x]\nfoo = "bar"\n\n[mcp_servers.context7]\ncommand = "npx"\n', encoding="utf-8")
    return install.ClientTarget("kimi-toml", "Kimi Code (config.toml)", config_path=path)


def test_install_kimi_toml_appends_block(tmp_path: Path) -> None:
    target = make_kimi_toml(tmp_path)
    result = install.install_kimi_toml(target, FAKE_LAUNCH, dry_run=False)
    assert result.ok and result.changed
    text = target.config_path.read_text()
    assert "[mcp_servers.extrovert]" in text
    assert 'command = "/usr/local/bin/extrovert"' in text
    assert "[mcp_servers.context7]" in text  # existing section kept
    assert target.config_path.with_name("config.toml.bak").exists()

    second = install.install_kimi_toml(target, FAKE_LAUNCH, dry_run=False)
    assert second.ok and not second.changed  # idempotent


def test_uninstall_kimi_toml_removes_block(tmp_path: Path) -> None:
    target = make_kimi_toml(tmp_path)
    install.install_kimi_toml(target, FAKE_LAUNCH, dry_run=False)
    result = install.uninstall_kimi_toml(target, dry_run=False)
    assert result.ok and result.changed
    text = target.config_path.read_text()
    assert "[mcp_servers.extrovert]" not in text
    assert "[mcp_servers.context7]" in text
    # removing again is a no-op
    again = install.uninstall_kimi_toml(target, dry_run=False)
    assert again.ok and not again.changed


# -- claude CLI (mocked subprocess) ------------------------------------------------


def make_claude_target() -> install.ClientTarget:
    return install.ClientTarget("claude-cli", "Claude Code", detail="/opt/claude")


def test_install_claude_registers_and_verifies() -> None:
    record: list[list[str]] = []
    runner = fake_runner_factory(record)
    result = install.install_claude(make_claude_target(), FAKE_LAUNCH, dry_run=False, runner=runner)
    # first `mcp list` (empty) -> add -> second `mcp list` (still empty) => not visible
    assert not result.ok

    record.clear()
    outputs = iter(["", "extrovert: /usr/local/bin/extrovert"])

    def runner2(cmd, **kwargs):
        record.append(list(cmd))
        out = subprocess.CompletedProcess(cmd, 0, "", "")
        if "list" in cmd:
            out.stdout = next(outputs)
        return out

    result = install.install_claude(make_claude_target(), FAKE_LAUNCH, dry_run=False, runner=runner2)
    assert result.ok and result.changed
    add_cmd = next(c for c in record if "add" in c)
    assert add_cmd[:5] == ["claude", "mcp", "add", "-s", "user"]
    assert add_cmd[5] == "extrovert"
    assert add_cmd[add_cmd.index("--") + 1 :] == ["/usr/local/bin/extrovert"]


def test_install_claude_already_registered() -> None:
    record: list[list[str]] = []
    runner = fake_runner_factory(record, list_output="extrovert: ...")
    result = install.install_claude(make_claude_target(), FAKE_LAUNCH, dry_run=False, runner=runner)
    assert result.ok and not result.changed
    assert all("add" not in c for c in record)


def test_uninstall_claude() -> None:
    record: list[list[str]] = []
    runner = fake_runner_factory(record, list_output="extrovert: ...")
    result = install.uninstall_claude(make_claude_target(), dry_run=False, runner=runner)
    assert result.ok and result.changed
    assert ["claude", "mcp", "remove", "-s", "user", "extrovert"] in record


def test_claude_dry_run_runs_no_commands() -> None:
    record: list[list[str]] = []
    runner = fake_runner_factory(record)
    result = install.install_claude(make_claude_target(), FAKE_LAUNCH, dry_run=True, runner=runner)
    assert result.ok and not result.changed
    assert record == []


# -- run_install orchestration -----------------------------------------------------


def test_run_install_no_clients_prints_generic_snippet(tmp_path: Path) -> None:
    lines: list[str] = []
    code = install.run_install(
        dry_run=False, home=tmp_path, which=fake_which({}), out=lines.append
    )
    assert code == 0
    text = "\n".join(lines)
    assert "no known agent client detected" in text
    assert '"mcpServers"' in text and '"extrovert"' in text


def test_run_install_manual_target_prints_snippet_and_touches_nothing(tmp_path: Path) -> None:
    kimi = tmp_path / ".kimi-code"
    kimi.mkdir()
    config = kimi / "config.toml"
    config.write_text('[providers.x]\nfoo = "bar"\n', encoding="utf-8")
    lines: list[str] = []
    code = install.run_install(dry_run=False, home=tmp_path, which=fake_which({}), out=lines.append)
    assert code == 0
    text = "\n".join(lines)
    assert "add this snippet yourself" in text
    assert "[mcp_servers.extrovert]" in text
    assert config.read_text() == '[providers.x]\nfoo = "bar"\n'  # untouched
    assert not (kimi / "config.toml.bak").exists()


def test_run_install_full_kimi_json(tmp_path: Path) -> None:
    kimi = tmp_path / ".kimi-code"
    kimi.mkdir()
    (kimi / "mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    code = install.run_install(dry_run=False, home=tmp_path, which=fake_which({}), out=lambda s: None)
    assert code == 0
    data = json.loads((kimi / "mcp.json").read_text())
    assert "extrovert" in data["mcpServers"]


# -- doctor -----------------------------------------------------------------------


def test_doctor_all_green(tmp_path: Path) -> None:
    code = doctor.run_doctor(
        which=fake_which({"npx": "/usr/bin/npx", "uvx": "/usr/bin/uvx"}),
        registry_probe=lambda: (True, "registry responded"),
        hint_probe=lambda: (True, "hint works"),
        state_home=tmp_path / "state",
        out=lambda s: None,
    )
    assert code == 0


def test_doctor_offline_registry_is_warning_not_failure(tmp_path: Path) -> None:
    lines: list[str] = []
    code = doctor.run_doctor(
        which=fake_which({}),
        registry_probe=lambda: (False, "boom"),
        hint_probe=lambda: (True, "ok"),
        state_home=tmp_path / "state",
        out=lines.append,
    )
    assert code == 0  # registry/npx/uvx are optional
    text = "\n".join(lines)
    assert "warn" in text and "registry" in text


def test_doctor_unwritable_state_dir_fails(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    lines: list[str] = []
    code = doctor.run_doctor(
        which=fake_which({}),
        registry_probe=lambda: (True, "ok"),
        hint_probe=lambda: (True, "ok"),
        state_home=blocker,
        out=lines.append,
    )
    assert code == 1
    assert "FAIL" in "\n".join(lines)


def test_doctor_probe_registry_uses_short_timeout() -> None:
    assert doctor.REGISTRY_PROBE_TIMEOUT <= 5


# -- CLI dispatch -------------------------------------------------------------------


def test_cli_parser_subcommands() -> None:
    parser = build_parser()
    assert parser.parse_args([]).subcommand is None
    assert parser.parse_args(["serve"]).subcommand == "serve"
    args = parser.parse_args(["install", "--dry-run"])
    assert args.subcommand == "install" and args.dry_run
    assert parser.parse_args(["uninstall", "-y"]).yes
    assert parser.parse_args(["doctor"]).subcommand == "doctor"
    assert parser.parse_args(["hint"]).subcommand == "hint"
    assert parser.parse_args(["install", "--with-hook"]).with_hook
    assert parser.parse_args(["uninstall", "--with-hook"]).with_hook
    assert not parser.parse_args(["install"]).with_hook


def test_cli_doctor_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "probe_registry", lambda: (True, "ok"))
    monkeypatch.setattr(doctor, "probe_hint", lambda: (True, "ok"))
    monkeypatch.setenv("EXTROVERT_HOME", str(tmp_path / "state"))
    with pytest.raises(SystemExit) as exc:
        main(["doctor"])
    assert exc.value.code == 0


def test_cli_install_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(install.shutil, "which", fake_which({}))
    monkeypatch.setattr(install.Path, "home", staticmethod(lambda: tmp_path))
    with pytest.raises(SystemExit) as exc:
        main(["install", "--dry-run"])
    assert exc.value.code == 0
    assert "mcpServers" in capsys.readouterr().out


# -- skill pack & magic prompt files -------------------------------------------------


def test_skill_md_frontmatter() -> None:
    assert SKILL_MD.exists(), "skills/extrovert/SKILL.md missing"
    text = SKILL_MD.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    frontmatter = text.split("---\n", 2)[1]
    fields = {}
    for line in frontmatter.strip().splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    assert fields["name"] == "extrovert"
    description = fields["description"]
    # situation-driven: the description describes task contexts, not user passwords
    assert "规划" in description
    assert "通用外部能力" in description
    assert "capability_consider" in description
    assert "无需用户明确要求" in description
    body = text.split("---\n", 2)[2]
    for tool in (
        "capability_consider",
        "capability_search",
        "capability_add",
        "capability_verify",
        "capability_list",
        "capability_remove",
    ):
        assert tool in body
    # planning checklist, honesty constraint, anti-patterns
    assert "规划期自检清单" in body
    assert "已确认无现成能力" in body
    assert "反模式" in body


# -- extrovert hint ----------------------------------------------------------------


def test_hint_text_single_line() -> None:
    from extrovert.hint import HINT_TEXT

    assert "\n" not in HINT_TEXT
    assert "capability_consider" in HINT_TEXT
    assert len(HINT_TEXT) < 120


def test_cli_hint(capsys) -> None:
    from extrovert.hint import HINT_TEXT

    with pytest.raises(SystemExit) as exc:
        main(["hint"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == HINT_TEXT


# -- claude UserPromptSubmit hook ----------------------------------------------------


def test_install_claude_hook_creates_settings(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    result = install.install_claude_hook(settings, FAKE_LAUNCH, dry_run=False)
    assert result.ok and result.changed
    data = json.loads(settings.read_text())
    entries = data["hooks"]["UserPromptSubmit"]
    assert entries == [{"hooks": [{"type": "command", "command": "/usr/local/bin/extrovert hint"}]}]
    # no backup when the file did not exist
    assert not settings.with_name("settings.json.bak").exists()


def test_install_claude_hook_preserves_existing_and_is_idempotent(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {
                "model": "opus",
                "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "other-tool hint"}]}]},
            }
        ),
        encoding="utf-8",
    )
    result = install.install_claude_hook(settings, FAKE_LAUNCH, dry_run=False)
    assert result.ok and result.changed
    data = json.loads(settings.read_text())
    assert data["model"] == "opus"
    commands = [
        h["command"]
        for entry in data["hooks"]["UserPromptSubmit"]
        for h in entry["hooks"]
    ]
    assert commands == ["other-tool hint", "/usr/local/bin/extrovert hint"]
    backup = settings.with_name("settings.json.bak")
    assert backup.exists()
    assert "extrovert" not in backup.read_text()

    again = install.install_claude_hook(settings, FAKE_LAUNCH, dry_run=False)
    assert again.ok and not again.changed


def test_install_claude_hook_dry_run_and_invalid_json(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{broken", encoding="utf-8")
    result = install.install_claude_hook(settings, FAKE_LAUNCH, dry_run=False)
    assert not result.ok and not result.changed
    assert "manually" in result.message

    settings.write_text("{}", encoding="utf-8")
    dry = install.install_claude_hook(settings, FAKE_LAUNCH, dry_run=True)
    assert dry.ok and not dry.changed
    assert json.loads(settings.read_text()) == {}  # untouched


def test_uninstall_claude_hook(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"env": {"A": "1"}}), encoding="utf-8")
    install.install_claude_hook(settings, FAKE_LAUNCH, dry_run=False)
    result = install.uninstall_claude_hook(settings, FAKE_LAUNCH, dry_run=False)
    assert result.ok and result.changed
    data = json.loads(settings.read_text())
    assert data == {"env": {"A": "1"}}  # empty hooks structures pruned

    again = install.uninstall_claude_hook(settings, FAKE_LAUNCH, dry_run=False)
    assert again.ok and not again.changed


def test_run_install_with_hook_writes_claude_settings(tmp_path: Path) -> None:
    record: list[list[str]] = []
    outputs = iter(["", "extrovert: ..."])

    def runner(cmd, **kwargs):
        record.append(list(cmd))
        out = subprocess.CompletedProcess(cmd, 0, "", "")
        if "list" in cmd:
            out.stdout = next(outputs)
        return out

    lines: list[str] = []
    code = install.run_install(
        dry_run=False,
        with_hook=True,
        home=tmp_path,
        which=fake_which({"claude": "/opt/claude", "extrovert": "/usr/local/bin/extrovert"}),
        runner=runner,
        out=lines.append,
    )
    assert code == 0
    settings = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    assert data["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] == "/usr/local/bin/extrovert hint"
    text = "\n".join(lines)
    assert "[hook]" in text


def test_run_install_with_hook_kimi_prints_snippet(tmp_path: Path) -> None:
    kimi = tmp_path / ".kimi-code"
    kimi.mkdir()
    (kimi / "mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    lines: list[str] = []
    code = install.run_install(
        dry_run=False,
        with_hook=True,
        home=tmp_path,
        which=fake_which({"extrovert": "/usr/local/bin/extrovert"}),
        out=lines.append,
    )
    assert code == 0
    text = "\n".join(lines)
    assert "hook 配置格式未确认" in text
    assert "/usr/local/bin/extrovert hint" in text
    assert not (tmp_path / ".claude").exists()  # no claude -> no settings written


# -- doctor hint check -----------------------------------------------------------------


def test_doctor_hint_check_optional_failure(tmp_path: Path) -> None:
    lines: list[str] = []
    code = doctor.run_doctor(
        which=fake_which({}),
        registry_probe=lambda: (True, "ok"),
        hint_probe=lambda: (False, "hint broken"),
        state_home=tmp_path / "state",
        out=lines.append,
    )
    assert code == 0  # hint is optional
    text = "\n".join(lines)
    assert "hint-command" in text and "hint broken" in text


def test_install_md_magic_prompt() -> None:
    assert INSTALL_MD.exists()
    text = INSTALL_MD.read_text(encoding="utf-8")
    assert "pip install git+https://github.com/yourlarok/extrovert.git" in text
    assert "extrovert doctor" in text
    assert "extrovert install" in text
    assert "extrovert 已就绪" in text
