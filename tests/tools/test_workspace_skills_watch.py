"""Tests for tools/workspace_skills_watch.py."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools import workspace_skills_watch as watch


def _event(path: Path | str, dest_path: Path | str | None = None):
    return SimpleNamespace(
        src_path=str(path),
        dest_path=str(dest_path) if dest_path is not None else None,
        event_type="modified",
        is_directory=False,
    )


class TestRunOnceStartup:
    def test_run_once_calls_sync_skills_with_workspace_params(self, tmp_path, monkeypatch):
        source = tmp_path / "workspace" / "skills"
        hermes_home = tmp_path / "hermes"
        source.mkdir(parents=True)
        monkeypatch.setenv("HERMES_WORKSPACE_SKILLS", str(source))
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        sync_mock = Mock(
            return_value={
                "copied": ["a"],
                "updated": [],
                "removed": 0,
                "skipped": 0,
                "user_modified": [],
                "total_bundled": 1,
            }
        )
        monkeypatch.setattr(watch, "sync_skills", sync_mock)

        result = watch.run_once("startup")

        sync_mock.assert_called_once_with(
            quiet=True,
            source_dir=source,
            target_dir=hermes_home / "skills",
            manifest_file=hermes_home / "skills" / ".workspace_manifest",
            remove_deleted=True,
        )
        assert result["reason"] == "startup"
        assert result["copied"] == 1
        assert result["total_source"] == 1


class TestDebounce:
    def test_five_events_within_quiet_window_trigger_one_sync(self, tmp_path):
        synced = threading.Event()
        calls = []

        def sync_func(reason, event_count):
            calls.append((reason, event_count))
            synced.set()

        handler = watch.DebouncedHandler(sync_func=sync_func)

        for i in range(5):
            handler.dispatch(_event(tmp_path / f"skill-{i}" / "SKILL.md"))
            time.sleep(0.2)

        assert synced.wait(3.0)
        time.sleep(0.2)

        assert calls == [("watch", 5)]


class TestPendingFollowup:
    def test_event_during_running_sync_schedules_one_followup(self, tmp_path):
        first_started = threading.Event()
        second_done = threading.Event()
        calls = []

        def sync_func(reason, event_count):
            calls.append((reason, event_count))
            if len(calls) == 1:
                first_started.set()
                time.sleep(0.15)
            if len(calls) == 2:
                second_done.set()

        handler = watch.DebouncedHandler(
            sync_func=sync_func,
            quiet_window=0.05,
            max_burst=1.0,
        )

        handler.dispatch(_event(tmp_path / "skill" / "SKILL.md"))
        assert first_started.wait(1.0)

        handler.dispatch(_event(tmp_path / "skill" / "README.md"))

        assert second_done.wait(1.0)
        assert len(calls) == 2
        assert calls[0] == ("watch", 1)
        assert calls[1] == ("watch", 1)


class TestLockExclusion:
    def test_second_acquire_fails_while_lock_is_owned(self, tmp_path):
        lock_file = tmp_path / ".workspace_sync.lock"

        assert watch.acquire_lock(lock_file) is True
        try:
            assert watch.acquire_lock(lock_file) is False
        finally:
            watch.release_lock(lock_file)

        assert not lock_file.exists()


class TestStaleLockRecovery:
    def test_stale_dead_pid_lock_is_recovered(self, tmp_path, monkeypatch):
        lock_file = tmp_path / ".workspace_sync.lock"
        lock_file.write_text("999999", encoding="utf-8")
        old = time.time() - watch.STALE_LOCK_SECONDS - 5
        os.utime(lock_file, (old, old))
        monkeypatch.setattr(watch, "_pid_exists", lambda pid: False)

        assert watch.acquire_lock(lock_file) is True
        try:
            assert lock_file.read_text(encoding="utf-8") == str(os.getpid())
        finally:
            watch.release_lock(lock_file)


class TestIgnoreFilter:
    @pytest.mark.parametrize(
        "path",
        [
            ".DS_Store",
            "file.swp",
            "file.swo",
            "file.tmp",
            "file~",
            ".git/HEAD",
            ".github/workflows/test.yml",
            "__pycache__/module.pyc",
        ],
    )
    def test_ignored_events_do_not_schedule_sync(self, tmp_path, path):
        synced = threading.Event()
        handler = watch.DebouncedHandler(
            sync_func=lambda reason, count: synced.set(),
            quiet_window=0.05,
            max_burst=1.0,
        )

        handler.dispatch(_event(tmp_path / path))

        assert not synced.wait(0.15)


class TestSymlinkLoop:
    def test_watch_loop_schedules_recursive_observer_without_following_symlinks(
        self,
        tmp_path,
        monkeypatch,
    ):
        source = tmp_path / "source"
        target = tmp_path / "target"
        source.mkdir()
        target.mkdir()
        (source / "loop").symlink_to(source, target_is_directory=True)

        config = watch.WatchConfig(
            source_dir=source,
            target_dir=target,
            manifest_file=target / ".workspace_manifest",
            lock_file=target / ".workspace_sync.lock",
        )
        stop_event = threading.Event()
        stop_event.set()
        scheduled = {}

        class FakeObserver:
            def schedule(self, handler, path, **kwargs):
                scheduled["handler"] = handler
                scheduled["path"] = path
                scheduled["kwargs"] = kwargs

            def start(self):
                scheduled["started"] = True

            def stop(self):
                scheduled["stopped"] = True

            def join(self):
                scheduled["joined"] = True

        monkeypatch.setattr(watch, "Observer", FakeObserver)
        monkeypatch.setattr(watch, "run_once", lambda *args, **kwargs: {})

        watch.watch_loop(config=config, stop_event=stop_event)

        assert scheduled["path"] == str(source)
        assert scheduled["kwargs"] == {"recursive": True}
        assert "follow_symlinks" not in scheduled["kwargs"]
        assert scheduled["started"] is True
        assert scheduled["stopped"] is True
        assert scheduled["joined"] is True
