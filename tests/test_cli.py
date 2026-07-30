from types import SimpleNamespace
from typing import Any

from folder2feishu import cli


def test_windowed_server_disables_uvicorn_stderr_formatter(
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    calls: dict[str, Any] = {}

    class FakeServices:
        def __init__(self) -> None:
            self.settings_store = SimpleNamespace(
                load=lambda: SimpleNamespace(port=8000, open_browser=False)
            )
            self.closed = False

        def close(self) -> None:
            self.closed = True

    services = FakeServices()
    monkeypatch.setattr(cli, "ApplicationServices", lambda *, paths: services)
    monkeypatch.setattr(cli, "create_app", lambda value: value)
    monkeypatch.setattr(cli, "_local_port_available", lambda port: True)

    def fake_run(app: Any, **kwargs: Any) -> None:
        calls["app"] = app
        calls.update(kwargs)

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)

    result = cli.main(["--no-browser", "--runtime-dir", str(tmp_path)])

    assert result == 0
    assert calls["host"] == "127.0.0.1"
    assert calls["port"] == 8000
    assert calls["log_config"] is None
    assert services.closed is True


def test_uninstall_command_removes_registered_schedules(
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    removed: list[str] = []

    class FakeServices:
        def __init__(self) -> None:
            self.schedules = SimpleNamespace(
                list_all=lambda: [
                    SimpleNamespace(project_id="project-one"),
                    SimpleNamespace(project_id="project-two"),
                ]
            )
            self.closed = False

        def close(self) -> None:
            self.closed = True

    services = FakeServices()
    monkeypatch.setattr(cli, "ApplicationServices", lambda *, paths: services)
    monkeypatch.setattr(cli, "remove_schedule", removed.append)

    result = cli.main(["--runtime-dir", str(tmp_path), "--remove-all-schedules"])

    assert result == 0
    assert removed == ["project-one", "project-two"]
    assert services.closed is True


def test_ui_mode_reports_port_conflict_without_starting_server(
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    messages: list[str] = []

    class FakeServices:
        def __init__(self) -> None:
            self.settings_store = SimpleNamespace(
                load=lambda: SimpleNamespace(port=8000, open_browser=False)
            )
            self.closed = False

        def close(self) -> None:
            self.closed = True

    services = FakeServices()
    monkeypatch.setattr(cli, "ApplicationServices", lambda *, paths: services)
    monkeypatch.setattr(cli, "_local_port_available", lambda port: False)
    monkeypatch.setattr(cli, "_show_startup_error", messages.append)
    monkeypatch.setattr(
        cli.uvicorn,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not start")),
    )

    result = cli.main(["--no-browser", "--runtime-dir", str(tmp_path)])

    assert result == 6
    assert "端口 8000 已被占用" in messages[0]
    assert services.closed is True
