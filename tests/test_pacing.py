"""Request pacing.

There were three separate cooldown implementations: a timestamp-based helper used
only by multi_turn, and a blind sleep in each of the other two modes. The blind
sleeps ignored time already spent, so a call taking 40s of a 60s cooldown waited
a further 60s instead of 20s. All three now share one helper.
"""
from __future__ import annotations

import asyncio

import pytest

from btcli import ai


@pytest.fixture
def fake_clock(monkeypatch):
    """Controllable clock and sleep, so pacing can be tested without waiting."""
    state = {"now": 1000.0, "slept": []}

    def clock():
        return state["now"]

    async def sleep(seconds):
        state["slept"].append(seconds)
        state["now"] += seconds

    monkeypatch.setattr(ai.time, "monotonic", clock)
    monkeypatch.setattr(ai.asyncio, "sleep", sleep)
    return state


def test_the_first_request_group_does_not_wait(fake_clock, isolated_settings):
    isolated_settings["PARALLEL_COOLDOWN"] = 60
    ai.reset_pacing()
    asyncio.run(ai.pace_requests())
    assert fake_clock["slept"] == []


def test_a_later_group_waits_the_full_cooldown_when_no_time_passed(
        fake_clock, isolated_settings):
    isolated_settings["PARALLEL_COOLDOWN"] = 60
    ai.reset_pacing()
    asyncio.run(ai.pace_requests())
    asyncio.run(ai.pace_requests())
    assert fake_clock["slept"] == [60]


def test_time_already_spent_counts_towards_the_cooldown(
        fake_clock, isolated_settings):
    """The blind sleeps used to ignore this and over-wait on every chunk."""
    isolated_settings["PARALLEL_COOLDOWN"] = 60
    ai.reset_pacing()
    asyncio.run(ai.pace_requests())

    fake_clock["now"] += 40          # the API call itself took 40s
    asyncio.run(ai.pace_requests())
    assert fake_clock["slept"] == [20], "should wait only the remaining 20s"


def test_no_wait_when_the_cooldown_has_already_elapsed(
        fake_clock, isolated_settings):
    isolated_settings["PARALLEL_COOLDOWN"] = 60
    ai.reset_pacing()
    asyncio.run(ai.pace_requests())

    fake_clock["now"] += 90          # slower than the cooldown
    asyncio.run(ai.pace_requests())
    assert fake_clock["slept"] == []


def test_a_zero_cooldown_never_waits(fake_clock, isolated_settings):
    isolated_settings["PARALLEL_COOLDOWN"] = 0
    ai.reset_pacing()
    for _ in range(3):
        asyncio.run(ai.pace_requests())
    assert fake_clock["slept"] == []


def test_a_negative_cooldown_is_treated_as_zero(fake_clock, isolated_settings):
    isolated_settings["PARALLEL_COOLDOWN"] = -30
    ai.reset_pacing()
    asyncio.run(ai.pace_requests())
    asyncio.run(ai.pace_requests())
    assert fake_clock["slept"] == []


def test_reset_clears_the_pacing_clock(fake_clock, isolated_settings):
    isolated_settings["PARALLEL_COOLDOWN"] = 60
    ai.reset_pacing()
    asyncio.run(ai.pace_requests())
    ai.reset_pacing()
    asyncio.run(ai.pace_requests())
    assert fake_clock["slept"] == [], "after a reset the next group starts clean"


def test_pacing_uses_a_monotonic_clock():
    """A wall-clock jump must not cause an hours-long wait or skip the cooldown."""
    import inspect

    source = inspect.getsource(ai.pace_requests)
    assert "monotonic" in source
    assert "time.time()" not in source


def test_every_mode_uses_the_shared_helper():
    """All three modes must pace through one code path."""
    import inspect

    for function in (ai.translate_chunked, ai.translate_multi_turn,
                     ai.translate_full_context):
        source = inspect.getsource(function)
        assert "pace_requests()" in source, function.__name__
        assert "PARALLEL_COOLDOWN" not in source, (
            f"{function.__name__} still has its own cooldown logic")
