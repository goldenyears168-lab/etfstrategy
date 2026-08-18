"""VPIN（Easley, López de Prado & O'Hara 2012）建構正確性。

釘住三件會讓 VPIN 靜默失去意義的事：成交量時鐘分桶、bulk volume
classification 的方向性、以及 vpin_at() 的因果切片邊界——最後這項是這個 repo
最貴的一次教訓（NQ 閘門同日 look-ahead 讓 CELL_TUNE_V2 的「5/5 顯著」變 0/5）。
"""

from __future__ import annotations

import random
import unittest

from tmf_channel.vpin import vpin_at, vpin_series, volume_buckets


class VolumeBucketTest(unittest.TestCase):
    def test_buckets_are_equal_volume_not_equal_time(self):
        # 前半每筆 1 口、後半每筆 10 口：等量分桶必須讓後半的桶「涵蓋較少筆數」
        px = list(range(100))
        vol = [1] * 50 + [10] * 50
        b = volume_buckets([float(p) for p in px], [float(v) for v in vol], 50.0)
        self.assertGreater(len(b), 2)
        for _dp, v in b:
            self.assertGreaterEqual(v, 50.0)

    def test_trailing_partial_bucket_is_dropped(self):
        b = volume_buckets([1.0, 2.0, 3.0], [10.0, 10.0, 1.0], 20.0)
        self.assertEqual(len(b), 1)  # 半個桶的失衡沒有可比性


class VpinValueTest(unittest.TestCase):
    def _series(self, gen, n=40000, seed=3):
        rng = random.Random(seed)
        px = [22000.0]
        for _ in range(n):
            px.append(gen(px[-1], rng))
        vol = [float(rng.randint(1, 5)) for _ in px]
        return px, vol

    def test_pure_trend_is_maximally_toxic(self):
        px, vol = self._series(lambda p, r: p + abs(r.gauss(2, 1)))
        s = [x for x in vpin_series(px, vol, buckets_per_day=200, window=50) if x is not None]
        self.assertTrue(s)
        self.assertGreater(min(s), 0.95)

    def test_random_walk_sits_near_one_half(self):
        px, vol = self._series(lambda p, r: p + r.gauss(0, 3))
        s = [x for x in vpin_series(px, vol, buckets_per_day=200, window=50) if x is not None]
        self.assertTrue(s)
        mid = sorted(s)[len(s) // 2]
        self.assertGreater(mid, 0.40)
        self.assertLess(mid, 0.60)

    def test_bounded_zero_to_one(self):
        px, vol = self._series(lambda p, r: p + r.gauss(0, 5))
        for x in vpin_series(px, vol, buckets_per_day=200, window=50):
            if x is not None:
                self.assertGreaterEqual(x, 0.0)
                self.assertLessEqual(x, 1.0)

    def test_degenerate_inputs_return_empty_not_crash(self):
        self.assertEqual(vpin_series([], []), [])
        self.assertEqual(vpin_series([1.0] * 5, [1.0] * 5), [])
        self.assertEqual(vpin_series([1.0] * 100, [0.0] * 100), [])


class CausalSliceTest(unittest.TestCase):
    def test_vpin_at_ignores_everything_after_the_index(self):
        rng = random.Random(11)
        px = [22000.0]
        for _ in range(30000):
            px.append(px[-1] + rng.gauss(0, 3))
        vol = [float(rng.randint(1, 5)) for _ in px]
        cut = 20000
        a = vpin_at(px, vol, cut, buckets_per_day=200, window=50)
        # 把切點之後的資料換成極端趨勢；因果的 VPIN 必須完全不動
        poisoned = px[:cut] + [px[cut - 1] + 500.0 * i for i in range(len(px) - cut)]
        b = vpin_at(poisoned, vol, cut, buckets_per_day=200, window=50)
        self.assertEqual(a, b)

    def test_too_early_returns_none(self):
        self.assertIsNone(vpin_at([1.0] * 50, [1.0] * 50, 5))


if __name__ == "__main__":
    unittest.main()
