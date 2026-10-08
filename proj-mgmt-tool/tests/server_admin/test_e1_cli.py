"""CLI integration exercises adapters without executing operating commands."""
import json
import pytest
import pmt.server_admin.cli as cli


@pytest.mark.parametrize("command", [
    ["plan", "--only", "service,firewall", "--allow-public"],
    ["apply", "--only", "dirs", "--apply"],
    ["service", "install", "--apply"],
    ["service", "restart"],
])
def test_operations_cli_passes_reviewable_arguments_without_system_execution(tmp_path, monkeypatch, capsys, command):
    captured = {}
    def handler(args, root, **kwargs):
        captured.update(command=args.command, root=str(root), apply=getattr(args, "apply", False),
                        action=getattr(args, "service_command", None))
        return {"ok": True, "applied": False, "components": []}
    monkeypatch.setattr(cli, "run_operations_command", handler)
    monkeypatch.setattr(cli, "run_service_command", handler)
    assert cli.main([*command, "--config-root", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    assert captured["command"] == command[0] and captured["root"] == str(tmp_path)
    assert not list(tmp_path.iterdir())


def test_failed_plan_or_apply_has_failure_exit(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "run_operations_command", lambda *_args, **_kw: {"ok": False, "failed_component": "service"})
    assert cli.main(["apply", "--apply", "--config-root", str(tmp_path), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert not list(tmp_path.iterdir())
