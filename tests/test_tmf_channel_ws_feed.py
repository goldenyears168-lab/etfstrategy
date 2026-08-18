"""Websocket candle feed · 有界記憶體與單次 handler 綁定。

2026-08-17 量測：worker 每輪耗時在啟動約 4.5 小時後由 0.16s 階梯式跳到 5.5s
且不再回復（1108 輪、18.1% 慢輪，且慢輪與 API 呼叫次數無關——81 個慢輪的
api_calls_this_poll=0）。2026-08-14 那次卡死 3 天 16 小時，也正是從同一個
6s 退化狀態發生的。這支測試釘住兩個候選機制。
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from order import tmf_channel_ws_feed as wsf


class _FakeWs:
    def __init__(self) -> None:
        self.handlers: list[tuple[str, object]] = []
        self.connects = 0
        self.subs: list[dict] = []

    def on(self, event: str, cb: object) -> None:
        self.handlers.append((event, cb))

    def connect(self) -> None:
        self.connects += 1

    def subscribe(self, payload: dict) -> None:
        self.subs.append(payload)


def _fake_session(ws: _FakeWs):
    s = mock.MagicMock()
    s.sdk.marketdata.websocket_client.futopt = ws
    return s


class HandlerBindingTest(unittest.TestCase):
    def test_reconnect_does_not_stack_message_handlers(self):
        ws = _FakeWs()
        feed = wsf.WebsocketCandleFeed(_fake_session(ws), "TMFH6")
        with mock.patch.dict("sys.modules", {"fubon_neo.adapter": mock.MagicMock()}):
            feed.start()
            for _ in range(5):  # simulate 5 disconnect→reconnect cycles
                feed._on_disconnect(1006, "gone")
                feed.start()
        msg_handlers = [h for e, h in ws.handlers if e == "message"]
        self.assertEqual(len(msg_handlers), 1, "each reconnect used to append another handler")
        self.assertEqual(ws.connects, 6)  # reconnects still happen

    def test_disconnect_still_marks_feed_not_started(self):
        ws = _FakeWs()
        feed = wsf.WebsocketCandleFeed(_fake_session(ws), "TMFH6")
        with mock.patch.dict("sys.modules", {"fubon_neo.adapter": mock.MagicMock()}):
            feed.start()
        self.assertTrue(feed._started)
        feed._on_disconnect(1006, "gone")
        self.assertFalse(feed._started)
        self.assertFalse(feed.fresh())


class RowPruningTest(unittest.TestCase):
    def _feed(self) -> wsf.WebsocketCandleFeed:
        return wsf.WebsocketCandleFeed(_fake_session(_FakeWs()), "TMFH6")

    def test_rows_are_bounded(self):
        feed = self._feed()
        for i in range(wsf._MAX_ROWS + 500):
            feed._on_message(json.dumps({
                "event": "data",
                "data": {"date": f"2026-08-17T{i // 60 % 24:02d}:{i % 60:02d}:00.000+08:00",
                         "open": 45000 + i, "high": 1, "low": 1, "close": 1},
            }))
        self.assertLessEqual(len(feed._rows), wsf._MAX_ROWS)

    def test_pruning_keeps_the_newest_minutes(self):
        feed = self._feed()
        with feed._lock:
            for i in range(wsf._MAX_ROWS + 10):
                feed._rows[f"k{i:06d}"] = {"i": i}
            feed._prune_locked()
        keys = sorted(feed._rows)
        self.assertEqual(len(keys), wsf._MAX_ROWS)
        self.assertEqual(keys[-1], f"k{wsf._MAX_ROWS + 9:06d}")
        self.assertNotIn("k000000", feed._rows)

    def test_snapshot_event_also_prunes(self):
        feed = self._feed()
        rows = [{"date": f"d{i:06d}", "open": 1, "high": 1, "low": 1, "close": 1}
                for i in range(wsf._MAX_ROWS + 300)]
        feed._on_message(json.dumps({"event": "snapshot", "data": {"data": rows}}))
        self.assertLessEqual(len(feed._rows), wsf._MAX_ROWS)

    def test_get_rows_is_sorted_and_intact(self):
        feed = self._feed()
        for d in ("2026-08-17T09:02:00", "2026-08-17T09:00:00", "2026-08-17T09:01:00"):
            feed._on_message(json.dumps({
                "event": "data", "data": {"date": d, "open": 1, "high": 1, "low": 1, "close": 1}}))
        self.assertEqual([r["date"] for r in feed.get_rows()],
                         ["2026-08-17T09:00:00", "2026-08-17T09:01:00", "2026-08-17T09:02:00"])


if __name__ == "__main__":
    unittest.main()
