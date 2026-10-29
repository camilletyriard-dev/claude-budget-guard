import multiprocessing as mp
import tempfile
import time
import unittest
from pathlib import Path

from claude_budget.ledger import BudgetExceeded, Ledger


def _reserve_one(path, results):
    try:
        Ledger(path).reserve("p", 1.0)
        results.put(True)
    except BudgetExceeded:
        results.put(False)


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.db"
        self.ledger = Ledger(self.path)
        self.ledger.set_budget("p", 10.0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reserve_settle_release(self):
        a = self.ledger.reserve("p", 4.0)
        self.assertAlmostEqual(self.ledger.spent("p"), 4.0)  # reservation counts
        self.ledger.settle(a, 1.5)
        self.assertAlmostEqual(self.ledger.spent("p"), 1.5)  # replaced by the actual cost
        b = self.ledger.reserve("p", 8.0)
        self.ledger.release(b)
        self.assertAlmostEqual(self.ledger.spent("p"), 1.5)

    def test_refuses_over_budget(self):
        self.ledger.reserve("p", 9.0)
        with self.assertRaises(BudgetExceeded):
            self.ledger.reserve("p", 1.01)
        self.ledger.reserve("p", 1.0)  # exactly fits

    def test_no_budget_means_no_cap(self):
        self.ledger.reserve("other", 1e6)
        self.assertIsNone(self.ledger.budget("other"))
        self.ledger.set_budget("p", None)
        self.ledger.reserve("p", 1e6)

    def test_manual_spend_and_corrections(self):
        self.ledger.add_manual("p", 9.5, "batch job")
        with self.assertRaises(BudgetExceeded):
            self.ledger.reserve("p", 1.0)
        self.ledger.add_manual("p", -2.0, "correction")
        self.ledger.reserve("p", 1.0)

    def test_run_limit(self):
        self.ledger.reserve("p", 2.0, run="r1", run_limit=3.0)
        with self.assertRaises(BudgetExceeded):
            self.ledger.reserve("p", 1.5, run="r1", run_limit=3.0)
        self.ledger.reserve("p", 1.5, run="r2", run_limit=3.0)  # other run unaffected
        self.assertAlmostEqual(self.ledger.run_spent("r1"), 2.0)

    def test_stale_reservations_expire(self):
        a = self.ledger.reserve("p", 9.0)
        with self.ledger._tx() as c:  # pretend the process crashed two hours ago
            c.execute("UPDATE entries SET ts=? WHERE id=?", (time.time() - 7200, a))
        self.ledger.reserve("p", 9.0)

    def test_summary(self):
        self.ledger.settle(self.ledger.reserve("p", 1.0), 0.25)
        self.ledger.add_manual("q", 1.0)
        self.assertEqual(self.ledger.summary(), [("p", 10.0, 0.25), ("q", None, 1.0)])

    def test_parallel_processes_never_overshoot(self):
        self.ledger.set_budget("p", 5.0)
        results = mp.Queue()
        procs = [mp.Process(target=_reserve_one, args=(self.path, results)) for _ in range(8)]
        for p in procs:
            p.start()
        for p in procs:
            p.join()
        outcomes = [results.get() for _ in procs]
        self.assertEqual(outcomes.count(True), 5)
        self.assertAlmostEqual(self.ledger.spent("p"), 5.0)


if __name__ == "__main__":
    unittest.main()
