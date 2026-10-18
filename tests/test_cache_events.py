import unittest

from token_dashboard import cache_events as ce

M = "m"


class TrackerTests(unittest.TestCase):
    def run_turns(self, turns):
        """turns: (sec, model, inp, read, c5, c1) -> list of steps."""
        t = ce.ThreadTracker()
        return [t.step(*x) for x in turns]

    def test_first_turn_is_never_expiry(self):
        s = self.run_turns([(0, M, 0, 0, 50000, 0)])[0]
        self.assertTrue(s.first)
        self.assertFalse(s.expired)

    def test_5m_expiry(self):
        a, b = self.run_turns([(0, M, 0, 0, 50000, 0), (301, M, 0, 0, 50000, 0)])
        self.assertTrue(b.expired)
        self.assertEqual(b.tier, "5m")
        self.assertEqual(b.cause, "ttl_expired_5m")
        self.assertAlmostEqual(b.gap_s, 301)

    def test_exactly_ttl_is_not_expired(self):
        _, b = self.run_turns([(0, M, 0, 0, 50000, 0), (300, M, 0, 0, 50000, 0)])
        self.assertFalse(b.expired)

    def test_1h_expiry_needs_1h_gap(self):
        _, mid, late = self.run_turns([(0, M, 0, 0, 0, 50000), (1800, M, 0, 0, 50000, 0),
                                       (1800 + 3601, M, 0, 0, 0, 50000)])
        self.assertFalse(mid.expired)      # last write was 1h, 30 min < 1h: still warm
        self.assertTrue(mid.miss)          # (a miss, but not a TTL one)
        self.assertTrue(late.expired)
        self.assertEqual(late.tier, "5m")  # the middle turn wrote 5m, so 5m TTL elapsed

    def test_1h_tier_expiry(self):
        _, b = self.run_turns([(0, M, 0, 0, 0, 50000), (3601, M, 0, 0, 0, 50000)])
        self.assertTrue(b.expired)
        self.assertEqual(b.tier, "1h")
        self.assertEqual(b.cause, "ttl_expired_1h")

    def test_1h_write_survives_long_5m_gap(self):
        _, b = self.run_turns([(0, M, 0, 0, 0, 50000), (1200, M, 0, 0, 50000, 0)])
        self.assertFalse(b.expired)

    def test_refresh_on_hit(self):
        # A hit at +200s refreshes the TTL, so +400s from the start is only a 200s gap.
        t = self.run_turns([(0, M, 0, 0, 50000, 0), (200, M, 0, 50000, 100, 0),
                            (400, M, 0, 50100, 100, 0)])
        self.assertFalse(any(s.expired for s in t))

    def test_gap_over_ttl_but_warm_usage_is_not_expiry(self):
        _, b = self.run_turns([(0, M, 0, 0, 50000, 0), (900, M, 0, 50000, 500, 0)])
        self.assertFalse(b.miss)
        self.assertFalse(b.expired)

    def test_model_switch_is_not_expiry(self):
        _, b = self.run_turns([(0, "a", 0, 0, 50000, 0), (900, "b", 0, 0, 50000, 0)])
        self.assertTrue(b.miss)
        self.assertEqual(b.cause, "model_switch")
        self.assertFalse(b.expired)

    def test_zero_token_turns_do_not_advance_state(self):
        a, z, b = self.run_turns([(0, M, 0, 0, 50000, 0), (250, M, 0, 0, 0, 0),
                                  (400, M, 0, 0, 50000, 0)])
        self.assertFalse(z.expired)
        self.assertTrue(b.expired)         # gap measured from the last real turn (400s)
        self.assertAlmostEqual(b.gap_s, 400)

    def test_unparseable_time_has_no_gap(self):
        t = ce.ThreadTracker()
        t.step(0, M, 0, 0, 50000, 0)
        b = t.step(None, M, 0, 0, 50000, 0)
        self.assertFalse(b.expired)
        self.assertIsNone(b.gap_s)

    def test_tier_mix_reported(self):
        _, b = self.run_turns([(0, M, 0, 0, 50000, 0), (400, M, 0, 0, 20000, 30000)])
        self.assertEqual((b.c5, b.c1), (20000, 30000))

    def test_epoch_matches_iso(self):
        self.assertAlmostEqual(ce.epoch("2026-04-10T12:05:00.000Z") - ce.epoch("2026-04-10T12:00:00.000Z"), 300)
        self.assertIsNone(ce.epoch("garbage"))


if __name__ == "__main__":
    unittest.main()
