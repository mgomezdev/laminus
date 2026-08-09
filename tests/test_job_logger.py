"""Regression: JobLogger must bound its buffered output instead of retaining every
line for the full JOB_TTL, and GET /api/slice/logs's cursor-based streaming must stay
correct once old lines have been evicted."""
import pytest

from app.main import JobLogger


async def _collect(logger: JobLogger) -> list[str]:
    lines = []
    async for chunk in logger.get_stream():
        lines.append(chunk)
    return lines


def test_log_buffer_is_bounded():
    logger = JobLogger("job-1", max_lines=10)
    for i in range(100):
        logger.log(f"line {i}")
    assert len(logger._logs) == 10
    assert logger._dropped == 90


@pytest.mark.asyncio
async def test_stream_replays_everything_when_under_the_limit():
    logger = JobLogger("job-2", max_lines=10)
    for i in range(3):
        logger.log(f"line {i}")
    logger.log("__COMPLETED__")

    lines = await _collect(logger)
    assert lines == [
        "data: line 0\n\n", "data: line 1\n\n", "data: line 2\n\n",
        "data: __COMPLETED__\n\n",
    ]


@pytest.mark.asyncio
async def test_stream_emits_dropped_marker_once_lines_are_evicted():
    logger = JobLogger("job-3", max_lines=5)
    for i in range(20):
        logger.log(f"line {i}")
    logger.log("__COMPLETED__")

    lines = await _collect(logger)
    # 21 messages logged (20 "line N" + terminal), buffer holds 5 -> 16 dropped.
    assert lines[0] == "data: __DROPPED__: 16 earlier line(s) omitted\n\n"
    assert lines[1:] == [
        "data: line 16\n\n", "data: line 17\n\n", "data: line 18\n\n",
        "data: line 19\n\n", "data: __COMPLETED__\n\n",
    ]
