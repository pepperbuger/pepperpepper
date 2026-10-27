"""SQLite 기록. 지금은 2단계용 테이블(괴리 샘플, 이벤트)만 있다.

SPEC 6번의 나머지 테이블(quote_snapshots, fills, hedges, funding, rewards)은
해당 단계를 구현할 때 같은 방식으로 추가한다.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from olbot.market import MarketState

SCHEMA = """
CREATE TABLE IF NOT EXISTS basis_samples (
    ts REAL NOT NULL,
    base TEXT NOT NULL,
    ondo_bid TEXT, ondo_ask TEXT,
    lighter_bid TEXT, lighter_ask TEXT,
    basis_bps REAL,
    ondo_age_s REAL, lighter_age_s REAL
);
CREATE INDEX IF NOT EXISTS basis_samples_base_ts ON basis_samples(base, ts);
CREATE TABLE IF NOT EXISTS events (
    ts REAL NOT NULL,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);
"""


class Ledger:
    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    def record_event(self, source: str, kind: str, detail: str = "") -> None:
        self.db.execute(
            "INSERT INTO events(ts, source, kind, detail) VALUES (?,?,?,?)",
            (time.time(), source, kind, detail),
        )
        self.db.commit()

    def record_sample(self, st: MarketState, now: float) -> None:
        o, l = st.ondo, st.lighter
        self.db.execute(
            "INSERT INTO basis_samples VALUES (?,?,?,?,?,?,?,?,?)",
            (
                now, st.base,
                str(o.bid) if o else None, str(o.ask) if o else None,
                str(l.bid) if l else None, str(l.ask) if l else None,
                st.basis_bps(),
                o.age(now) if o else None, l.age(now) if l else None,
            ),
        )

    def commit(self) -> None:
        self.db.commit()

    def basis_summary(self, since: float) -> list[tuple]:
        """마켓별 (base, 샘플 수, 양쪽 다 있는 샘플 수, 평균, 최소, 최대 괴리)."""
        return self.db.execute(
            """SELECT base, COUNT(*), COUNT(basis_bps),
                      AVG(basis_bps), MIN(basis_bps), MAX(basis_bps)
               FROM basis_samples WHERE ts >= ? GROUP BY base ORDER BY base""",
            (since,),
        ).fetchall()

    def event_counts(self, since: float) -> list[tuple]:
        return self.db.execute(
            "SELECT source, kind, COUNT(*) FROM events WHERE ts >= ? GROUP BY source, kind ORDER BY source, kind",
            (since,),
        ).fetchall()

    def basis_values(self, base: str, since: float) -> list[float]:
        rows = self.db.execute(
            "SELECT basis_bps FROM basis_samples WHERE base=? AND ts>=? AND basis_bps IS NOT NULL ORDER BY ts",
            (base, since),
        ).fetchall()
        return [r[0] for r in rows]

    def max_sample_gap(self, base: str, since: float) -> float:
        """양쪽 호가가 모두 있는 샘플 사이의 가장 긴 공백(초)."""
        rows = self.db.execute(
            "SELECT ts FROM basis_samples WHERE base=? AND ts>=? AND basis_bps IS NOT NULL ORDER BY ts",
            (base, since),
        ).fetchall()
        gaps = [b[0] - a[0] for a, b in zip(rows, rows[1:])]
        return max(gaps) if gaps else 0.0
