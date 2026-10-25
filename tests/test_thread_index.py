import os
import tempfile
import unittest

from token_dashboard.db import connect, init_db


class ThreadIndexTests(unittest.TestCase):
    def test_thread_lookup_uses_the_index(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.db")
            init_db(db)
            with connect(db) as c:
                plan = " ".join(r[3] for r in c.execute(
                    """EXPLAIN QUERY PLAN SELECT uuid FROM messages
                        WHERE session_id=? AND COALESCE(agent_id,'')=? AND type='assistant'
                        ORDER BY timestamp, rowid""", ("s", "")))
        self.assertIn("idx_messages_thread", plan)


if __name__ == "__main__":
    unittest.main()
