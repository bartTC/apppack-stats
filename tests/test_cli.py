from __future__ import annotations

import io
import threading

import apppack_stats


class FakeProc:
    def __init__(self) -> None:
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")
        self.returncode = 0
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return None if not self.terminated and not self.killed else 0

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class FakeThread:
    def __init__(self, target, args, daemon) -> None:
        self.target = target
        self.args = args
        self.daemon = daemon
        self.started = False

    def start(self) -> None:
        self.started = True


class FakeStatsApp:
    last_instance: FakeStatsApp | None = None

    def __init__(self, stats, *, app_name: str, refresh: float, proc) -> None:
        self.stats = stats
        self.app_name = app_name
        self.refresh = refresh
        self.proc = proc
        self.sort_col = "avg"
        self.sort_reverse = True
        self.exit_error = None
        FakeStatsApp.last_instance = self

    def run(self) -> None:
        return None


def test_main_returns_2_when_apppack_cli_is_missing(monkeypatch, capsys) -> None:
    monkeypatch.setattr(apppack_stats.shutil, "which", lambda _: None)

    rc = apppack_stats.main(["demo-app"])

    err = capsys.readouterr().err
    assert rc == 2
    assert "apppack' CLI not found" in err


def test_main_uses_fake_process_and_passes_app_name(monkeypatch) -> None:
    fake_proc = FakeProc()

    monkeypatch.setattr(apppack_stats.shutil, "which", lambda _: "/usr/bin/apppack")
    monkeypatch.setattr(
        apppack_stats.subprocess, "Popen", lambda *_args, **_kwargs: fake_proc
    )
    monkeypatch.setattr(threading, "Thread", FakeThread)
    monkeypatch.setattr(apppack_stats, "StatsApp", FakeStatsApp)

    rc = apppack_stats.main(["demo-app", "--refresh", "0.5"])

    assert rc == 0
    assert FakeStatsApp.last_instance is not None
    assert FakeStatsApp.last_instance.app_name == "demo-app"
    assert FakeStatsApp.last_instance.refresh == 0.5
    assert fake_proc.terminated is True
