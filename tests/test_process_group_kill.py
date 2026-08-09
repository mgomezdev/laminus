"""Regression: on timeout, the whole OrcaSlicer process group must be killed, not
just the direct xvfb-run wrapper (a /bin/sh script that can't be trapped and would
otherwise leave Xvfb + orcaslicer running, reparented to PID 1)."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.main import _kill_process_group


@pytest.mark.asyncio
async def test_kills_process_group_when_killpg_succeeds(monkeypatch):
    # signal.SIGKILL/os.killpg are POSIX-only (absent on Windows, where these
    # tests also run) - patch both so the real code path is exercised everywhere.
    monkeypatch.setattr("signal.SIGKILL", 9, raising=False)
    calls = []
    monkeypatch.setattr("os.killpg", lambda pid, sig: calls.append((pid, sig)), raising=False)
    proc = AsyncMock()
    proc.pid = 12345
    proc.kill = MagicMock()

    await _kill_process_group(proc)

    assert calls == [(12345, 9)]
    proc.kill.assert_not_called()
    proc.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_falls_back_to_process_kill_when_killpg_unavailable(monkeypatch):
    monkeypatch.setattr("signal.SIGKILL", 9, raising=False)

    def _raise(pid, sig):
        raise ProcessLookupError()
    monkeypatch.setattr("os.killpg", _raise, raising=False)
    proc = AsyncMock()
    proc.pid = 12345
    proc.kill = MagicMock()

    await _kill_process_group(proc)

    proc.kill.assert_called_once()
    proc.wait.assert_awaited_once()
