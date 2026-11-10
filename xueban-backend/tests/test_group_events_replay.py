"""Group SSE disconnect replay (2026-11-07).

Covers the xueban/api/_group_events.py seq + ring buffer + Last-Event-ID
cursor catch-up — the Python half of the same-day Node change
(server/src/routes/groups-event-bus.ts + groups-node.ts). Motivation: frames
published while a client's SSE connection was down were dropped forever (the
bus was a pure in-memory pub/sub with no buffering); a reconnecting browser
now replays the missed frames via its Last-Event-ID cursor.

Scope: bus primitives (ids / buffer / eviction / typing skip / cursor rules),
the pure endpoint helpers (_format_group_event / _parse_replay_cursor), and
the stream_group_events generator wiring (replay-before-live, overlap dedupe).

Data isolation: _GROUPS_DIR monkeypatched to tmp_path and the bus reset between
tests (same pattern as test_group_chat_realtime.py).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from xueban.api import _group_events
from xueban.api import groups as groups_mod


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    g = tmp_path / "groups"
    g.mkdir()
    monkeypatch.setattr(groups_mod, "_GROUPS_DIR", g)
    _group_events.reset_for_tests()
    yield
    _group_events.reset_for_tests()


# ---------------------------------------------------------------------------
# Bus: ids + replay buffer
# ---------------------------------------------------------------------------


class TestPublishAssignsIds:
    def test_ids_monotonic_per_group(self):
        a = _group_events.publish_group_event("g1", {"type": "agent.message", "data": {}})
        b = _group_events.publish_group_event("g1", {"type": "agent.message", "data": {}})
        assert (a, b) == (0, 1)

    def test_groups_have_independent_seqs(self):
        a = _group_events.publish_group_event("g1", {"type": "agent.message", "data": {}})
        b = _group_events.publish_group_event("g2", {"type": "agent.message", "data": {}})
        assert (a, b) == (0, 0)

    def test_event_copy_carries_id_caller_dict_untouched(self):
        original: Dict[str, Any] = {"type": "agent.message", "data": {"m": 1}}
        q = _group_events.subscribe("g1")
        _group_events.publish_group_event("g1", original)
        assert "id" not in original
        assert q.get_nowait()["id"] == 0

    def test_typing_frames_still_get_ids(self):
        _group_events.publish_group_event("g1", {"type": "agent.typing", "data": {}})
        assert _group_events.latest_seq("g1") == 0


class TestReplayBuffer:
    def test_buffered_even_without_subscribers(self):
        """断线窗口（无订阅者）的帧不再丢：入缓冲待补。"""
        _group_events.publish_group_event("g1", {"type": "agent.message", "data": {"m": 1}})
        assert [e["id"] for e in _group_events.get_group_events_after("g1")] == [0]

    def test_typing_not_buffered(self):
        _group_events.publish_group_event("g1", {"type": "agent.typing", "data": {}})
        _group_events.publish_group_event("g1", {"type": "agent.message", "data": {}})
        assert [e["id"] for e in _group_events.get_group_events_after("g1")] == [1]

    def test_buffer_survives_unsubscribe(self):
        q = _group_events.subscribe("g1")
        _group_events.publish_group_event("g1", {"type": "agent.message", "data": {}})
        _group_events.unsubscribe("g1", q)
        assert _group_events.subscriber_count("g1") == 0
        assert [e["id"] for e in _group_events.get_group_events_after("g1")] == [0]

    def test_ring_buffer_evicts_oldest(self):
        for i in range(_group_events._REPLAY_BUFFER_MAX + 20):
            _group_events.publish_group_event("g1", {"type": "agent.message", "data": {"i": i}})
        ids = [e["id"] for e in _group_events.get_group_events_after("g1")]
        assert len(ids) == _group_events._REPLAY_BUFFER_MAX
        assert ids[0] == 20  # 最旧 20 条被驱逐
        assert ids[-1] == _group_events._REPLAY_BUFFER_MAX + 19

    def test_seq_monotonic_regardless_of_eviction(self):
        for _ in range(_group_events._REPLAY_BUFFER_MAX + 5):
            seq = _group_events.publish_group_event("g1", {"type": "agent.message", "data": {}})
        assert seq == _group_events._REPLAY_BUFFER_MAX + 5 - 1

    def test_tracked_group_cap_evicts_whole_oldest_buffer(self):
        cap = _group_events._MAX_TRACKED_GROUPS
        for i in range(cap + 2):
            _group_events.publish_group_event(f"g{i}", {"type": "agent.message", "data": {}})
        assert len(_group_events._replay_buffers) == cap
        # 最旧的两组整组被驱逐（g0/g1），最新一组仍在
        assert _group_events.get_group_events_after("g0") == []
        assert [e["id"] for e in _group_events.get_group_events_after(f"g{cap + 1}")] == [0]


class TestGetAfterCursor:
    def _seed(self):
        for i in range(4):
            _group_events.publish_group_event("g1", {"type": "agent.message", "data": {"i": i}})

    def test_cursor_filters(self):
        self._seed()
        assert [e["id"] for e in _group_events.get_group_events_after("g1", 1)] == [2, 3]

    def test_cursor_none_replays_all(self):
        self._seed()
        assert [e["id"] for e in _group_events.get_group_events_after("g1", None)] == [0, 1, 2, 3]

    def test_stale_high_cursor_replays_all(self):
        """进程重启后 seq 归零：旧游标越界 → 视同无游标全量补发（客户端幂等去重）。"""
        self._seed()
        assert [e["id"] for e in _group_events.get_group_events_after("g1", 42)] == [0, 1, 2, 3]

    def test_cursor_at_watermark_replays_nothing(self):
        self._seed()
        assert _group_events.get_group_events_after("g1", 3) == []

    def test_gap_from_eviction_replays_remaining(self):
        for i in range(_group_events._REPLAY_BUFFER_MAX + 10):
            _group_events.publish_group_event("g1", {"type": "agent.message", "data": {}})
        # 游标落在被驱逐区间：补不回的宁可缺，不重复
        ids = [e["id"] for e in _group_events.get_group_events_after("g1", 2)]
        assert ids[0] == 10
        assert len(ids) == _group_events._REPLAY_BUFFER_MAX


# ---------------------------------------------------------------------------
# Endpoint helpers (pure)
# ---------------------------------------------------------------------------


class TestFormatGroupEvent:
    def test_frame_with_id(self):
        frame = groups_mod._format_group_event({"id": 7, "type": "agent.message", "data": {"m": 1}})
        assert frame.startswith("id: 7\n")
        assert "\nevent: agent.message\n" in frame
        assert '\ndata: {"m": 1}\n\n' in frame

    def test_frame_without_id_has_no_id_line(self):
        frame = groups_mod._format_group_event({"type": "system.notice", "data": {}})
        assert not frame.startswith("id:")
        assert frame.startswith("event: system.notice\n")


class TestParseReplayCursor:
    def _req(self, headers=None, query=None):
        return SimpleNamespace(headers=headers or {}, query_params=query or {})

    def test_header_wins(self):
        req = self._req(headers={"last-event-id": "5"}, query={"last_event_id": "9"})
        assert groups_mod._parse_replay_cursor(req) == 5

    def test_query_fallbacks(self):
        assert groups_mod._parse_replay_cursor(self._req(query={"last_event_id": "3"})) == 3
        assert groups_mod._parse_replay_cursor(self._req(query={"after": "4"})) == 4

    def test_invalid_returns_none(self):
        assert groups_mod._parse_replay_cursor(self._req(headers={"last-event-id": "abc"})) is None
        assert groups_mod._parse_replay_cursor(self._req(query={"after": "-1"})) is None
        assert groups_mod._parse_replay_cursor(self._req()) is None


# ---------------------------------------------------------------------------
# Endpoint generator: replay-before-live + overlap dedupe
# ---------------------------------------------------------------------------


def _fake_request(cursor_calls: List[bool], headers=None, query=None):
    """Fake Starlette Request: only is_disconnected() + cursor parsing are used."""

    async def is_disconnected() -> bool:
        return cursor_calls.pop(0) if cursor_calls else True

    return SimpleNamespace(
        headers=headers or {},
        query_params=query or {},
        is_disconnected=is_disconnected,
    )


async def _pull(iterator, n: int) -> List[str]:
    out: List[str] = []
    for _ in range(n):
        out.append(await iterator.__anext__())
    return out


def _make_group(group_id: str) -> None:
    groups_mod._save_group(
        {
            "group_id": group_id,
            "name": "Replay Group",
            "agent_ids": [],
            "messages": [],
        }
    )


class TestStreamGroupEventsReplay:
    def test_fresh_connect_replays_buffer(self):
        _make_group("g1")
        _group_events.publish_group_event("g1", {"type": "agent.message", "data": {"m": "A"}})
        _group_events.publish_group_event("g1", {"type": "agent.message", "data": {"m": "B"}})

        resp = asyncio.run(groups_mod.stream_group_events("g1", _fake_request([True])))

        async def run():
            frames = await _pull(resp.body_iterator, 3)
            await resp.body_iterator.aclose()
            return frames

        frames = asyncio.run(run())
        assert frames[0] == ": connected\n\n"
        assert frames[1].startswith("id: 0\n") and '"m": "A"' in frames[1]
        assert frames[2].startswith("id: 1\n") and '"m": "B"' in frames[2]

    def test_cursor_replays_only_missed(self):
        _make_group("g1")
        for i in range(3):
            _group_events.publish_group_event("g1", {"type": "agent.message", "data": {"i": i}})

        req = _fake_request([True], headers={"last-event-id": "0"})
        resp = asyncio.run(groups_mod.stream_group_events("g1", req))

        async def run():
            frames = await _pull(resp.body_iterator, 3)
            await resp.body_iterator.aclose()
            return frames

        frames = asyncio.run(run())
        assert frames[0] == ": connected\n\n"
        assert frames[1].startswith("id: 1\n")
        assert frames[2].startswith("id: 2\n")

    def test_stale_high_cursor_replays_whole_buffer(self):
        _make_group("g1")
        _group_events.publish_group_event("g1", {"type": "agent.message", "data": {"m": "A"}})

        req = _fake_request([True], headers={"last-event-id": "42"})
        resp = asyncio.run(groups_mod.stream_group_events("g1", req))

        async def run():
            frames = await _pull(resp.body_iterator, 2)
            await resp.body_iterator.aclose()
            return frames

        frames = asyncio.run(run())
        assert frames[0] == ": connected\n\n"
        assert frames[1].startswith("id: 0\n")

    def test_overlap_event_delivered_exactly_once(self):
        """订阅与补帧快照之间 publish 的帧：回放与 live 两路都到，水位去重只发一次。"""
        _make_group("g1")
        _group_events.publish_group_event("g1", {"type": "agent.message", "data": {"m": "A"}})

        # is_disconnected：主循环第 1 圈 False（走 live 队列）→ 第 2 圈 True（收场）
        resp = asyncio.run(groups_mod.stream_group_events("g1", _fake_request([False, True])))

        async def run():
            frames = [await resp.body_iterator.__anext__()]  # : connected
            # 生成器此刻已订阅但尚未快照 → C 同时进缓冲与队列
            _group_events.publish_group_event("g1", {"type": "agent.message", "data": {"m": "C"}})
            frames.append(await resp.body_iterator.__anext__())  # replay A
            frames.append(await resp.body_iterator.__anext__())  # replay C
            # 下一次恢复进主循环：live 队列里的 C 被水位去重 → 直接收场
            ended = False
            try:
                frames.append(await resp.body_iterator.__anext__())
            except StopAsyncIteration:
                ended = True
            return frames, ended

        frames, ended = asyncio.run(run())
        assert ended, "去重后不应再有帧，主循环应干净收场"
        bodies = [f for f in frames if f != ": connected\n\n"]
        assert len(bodies) == 2, f"overlap frame must appear once: {bodies!r}"
        assert bodies[0].startswith("id: 0\n")
        assert bodies[1].startswith("id: 1\n")

    def test_typing_never_replayed(self):
        _make_group("g1")
        _group_events.publish_group_event("g1", {"type": "agent.typing", "data": {"agent": "a"}})
        _group_events.publish_group_event("g1", {"type": "agent.message", "data": {"m": "B"}})

        resp = asyncio.run(groups_mod.stream_group_events("g1", _fake_request([True])))

        async def run():
            frames = await _pull(resp.body_iterator, 2)
            await resp.body_iterator.aclose()
            return frames

        frames = asyncio.run(run())
        assert frames[0] == ": connected\n\n"
        assert frames[1].startswith("id: 1\n")  # typing 占了 id 0 但不回放
        assert all("agent.typing" not in f for f in frames)

    def test_unsubscribe_on_close(self):
        _make_group("g1")
        resp = asyncio.run(groups_mod.stream_group_events("g1", _fake_request([True])))
        assert _group_events.subscriber_count("g1") == 0  # 生成器未驱动时不订阅

        async def run():
            await _pull(resp.body_iterator, 1)  # 首拉才 subscribe
            assert _group_events.subscriber_count("g1") == 1
            await resp.body_iterator.aclose()

        asyncio.run(run())
        assert _group_events.subscriber_count("g1") == 0
