"""Local SQLite ledger: budgets per project and every call's cost.

Reserve-then-settle keeps parallel processes on one machine from overshooting:
the worst-case cost is reserved atomically before a call, and replaced by the
actual cost afterwards. One ledger per machine; it is not shared across
machines (the watchdog covers that).
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

STALE_RESERVATION_S = 3600  # reservations from crashed processes stop counting after 1 h


class BudgetExceeded(RuntimeError):
    """Raised before a call is sent when its worst-case cost would exceed a budget."""

    def __init__(self, message: str, scope: str = "project"):
        super().__init__(message)
        self.scope = scope  # "project" or "run"


def default_path() -> Path:
    return Path(os.environ.get("CLAUDE_BUDGET_DB", Path.home() / ".claude_budget" / "ledger.db"))


class Ledger:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or default_path())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        with conn:  # executescript manages its own transaction
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS budgets(project TEXT PRIMARY KEY, budget_usd REAL);
                CREATE TABLE IF NOT EXISTS entries(
                    id INTEGER PRIMARY KEY, ts REAL, project TEXT, run TEXT, model TEXT,
                    status TEXT, reserved_usd REAL DEFAULT 0, cost_usd REAL DEFAULT 0, note TEXT);
                CREATE INDEX IF NOT EXISTS entries_project ON entries(project, status);
                CREATE TABLE IF NOT EXISTS alerts(project TEXT, level REAL, budget_usd REAL,
                                                  PRIMARY KEY(project, level, budget_usd));
            """)
        conn.close()

    @contextmanager
    def _tx(self):
        """One connection per transaction; BEGIN IMMEDIATE takes the write lock up front."""
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # committed = settled + manual costs + live reservations
    _COMMITTED = """SELECT COALESCE(SUM(CASE WHEN status='reserved' THEN reserved_usd ELSE cost_usd END), 0)
                    FROM entries WHERE status IN ('settled','manual','reserved') AND {where}"""

    def _committed(self, c, where: str, arg: str) -> float:
        return c.execute(self._COMMITTED.format(where=where), (arg,)).fetchone()[0]

    def set_budget(self, project: str, usd: float | None) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO budgets VALUES(?,?) ON CONFLICT(project) DO UPDATE SET budget_usd=excluded.budget_usd",
                      (project, usd))

    def budget(self, project: str) -> float | None:
        with self._tx() as c:
            row = c.execute("SELECT budget_usd FROM budgets WHERE project=?", (project,)).fetchone()
        return row[0] if row else None

    def spent(self, project: str) -> float:
        with self._tx() as c:
            return self._committed(c, "project=?", project)

    def run_spent(self, run: str) -> float:
        with self._tx() as c:
            return self._committed(c, "run=?", run)

    def reserve(self, project: str, usd: float, model: str = "", run: str | None = None,
                run_limit: float | None = None) -> int:
        """Atomically check budgets and hold `usd`. Raises BudgetExceeded if it does not fit."""
        with self._tx() as c:
            c.execute("UPDATE entries SET status='released', note='stale reservation' "
                      "WHERE status='reserved' AND ts < ?", (time.time() - STALE_RESERVATION_S,))
            row = c.execute("SELECT budget_usd FROM budgets WHERE project=?", (project,)).fetchone()
            budget = row[0] if row else None
            if budget is not None:
                left = budget - self._committed(c, "project=?", project)
                if usd > left + 1e-12:
                    raise BudgetExceeded(f"Project '{project}': worst case ${usd:.4f} > remaining ${left:.4f} "
                                         f"of ${budget:.2f}. Call not sent.")
            if run is not None and run_limit is not None:
                left = run_limit - self._committed(c, "run=?", run)
                if usd > left + 1e-12:
                    raise BudgetExceeded(f"Run budget: worst case ${usd:.4f} > remaining ${left:.4f} "
                                         f"of ${run_limit:.2f}. Call not sent.", scope="run")
            return c.execute("INSERT INTO entries(ts, project, run, model, status, reserved_usd) "
                             "VALUES(?,?,?,?, 'reserved', ?)", (time.time(), project, run, model, usd)).lastrowid

    def settle(self, entry_id: int, cost: float, note: str | None = None) -> None:
        with self._tx() as c:
            c.execute("UPDATE entries SET status='settled', cost_usd=?, note=? WHERE id=?", (cost, note, entry_id))

    def release(self, entry_id: int, note: str | None = None) -> None:
        with self._tx() as c:
            c.execute("UPDATE entries SET status='released', note=? WHERE id=?", (note, entry_id))

    def add_manual(self, project: str, usd: float, note: str | None = None) -> None:
        """Record spend made outside the wrapper, or a correction (negative is allowed)."""
        with self._tx() as c:
            c.execute("INSERT INTO entries(ts, project, status, cost_usd, note) VALUES(?,?, 'manual', ?, ?)",
                      (time.time(), project, usd, note))

    def first_alert(self, project: str, level: float, budget: float) -> bool:
        """True exactly once per (project, level, budget), across processes. A new budget re-arms alerts."""
        with self._tx() as c:
            return c.execute("INSERT OR IGNORE INTO alerts VALUES(?,?,?)", (project, level, budget)).rowcount == 1

    def spent_since(self, project: str, since_ts: float) -> float:
        with self._tx() as c:
            return c.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM entries WHERE project=? AND ts>=? "
                             "AND status IN ('settled','manual')", (project, since_ts)).fetchone()[0]

    def summary(self) -> list[tuple[str, float | None, float]]:
        """(project, budget, spent) for every known project."""
        with self._tx() as c:
            projects = [r[0] for r in c.execute(
                "SELECT project FROM budgets UNION SELECT DISTINCT project FROM entries ORDER BY 1")]
            budgets = dict(c.execute("SELECT project, budget_usd FROM budgets").fetchall())
            return [(p, budgets.get(p), self._committed(c, "project=?", p)) for p in projects]

    def history(self, project: str, n: int = 20) -> list[tuple]:
        with self._tx() as c:
            return c.execute("SELECT ts, run, model, status, reserved_usd, cost_usd, note FROM entries "
                             "WHERE project=? ORDER BY id DESC LIMIT ?", (project, n)).fetchall()
