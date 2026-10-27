"""SQLite 운영 기록과 재시작 후 복원할 미확정 헷지 상태."""

from __future__ import annotations

import sqlite3
import time
from decimal import Decimal
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
CREATE TABLE IF NOT EXISTS fills (
    ts REAL NOT NULL, base TEXT NOT NULL, fill_id TEXT, side TEXT,
    price TEXT, size TEXT, fee TEXT, is_maker INTEGER,
    ondo_mid TEXT, lighter_mid TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS fills_id ON fills(fill_id);
CREATE TABLE IF NOT EXISTS hedges (
    ts REAL NOT NULL, base TEXT NOT NULL, size TEXT, side TEXT,
    worst_price TEXT, lighter_mid TEXT, filled TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS quote_snapshots (
    ts REAL NOT NULL, base TEXT NOT NULL, side TEXT,
    price TEXT, size TEXT, notional_usd REAL, dist_bps REAL, in_band INTEGER, interval_s REAL
);
CREATE TABLE IF NOT EXISTS equity (
    ts REAL NOT NULL, ondo_usd TEXT, lighter_usd TEXT, total_usd TEXT
);
CREATE TABLE IF NOT EXISTS rewards (
    ts REAL NOT NULL, week_start TEXT, week_end TEXT, usdc TEXT, points TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS pending_hedges (
    scope TEXT NOT NULL, base TEXT NOT NULL,
    size TEXT NOT NULL, is_sell INTEGER NOT NULL,
    lighter_pos_before TEXT NOT NULL, sent_ts REAL NOT NULL,
    PRIMARY KEY (scope, base)
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

    def save_pending_hedge(self, scope: str, base: str, pending) -> None:
        """전송 전 미확정을 영속화한다. scope는 거래소 URL+계정 번호이며 키는 저장하지 않는다."""
        if pending is None:
            self.db.execute("DELETE FROM pending_hedges WHERE scope=? AND base=?", (scope, base))
        else:
            self.db.execute(
                "INSERT OR REPLACE INTO pending_hedges VALUES (?,?,?,?,?,?)",
                (scope, base, str(pending.size), int(pending.is_sell),
                 str(pending.lighter_pos_before), pending.sent_ts),
            )
        self.db.commit()

    def load_pending_hedges(self, scope: str) -> dict:
        from olbot.hedger import PendingHedge

        rows = self.db.execute(
            "SELECT base,size,is_sell,lighter_pos_before,sent_ts FROM pending_hedges WHERE scope=?", (scope,)
        ).fetchall()
        return {base: PendingHedge(Decimal(size), bool(is_sell), Decimal(before), sent_ts)
                for base, size, is_sell, before, sent_ts in rows}

    def record_fill(self, base: str, fill: dict, ondo_mid, lighter_mid) -> bool:
        """Ondo 체결 한 건. 같은 fill id가 다시 오면 무시하고 False."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO fills VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), base, fill.get("id"), fill.get("side"), fill.get("price"), fill.get("size"),
             fill.get("fee"), 1 if fill.get("isMaker") else 0,
             str(ondo_mid) if ondo_mid is not None else None, str(lighter_mid) if lighter_mid is not None else None),
        )
        self.db.commit()
        return cur.rowcount > 0

    def record_hedge(self, base, size, is_sell, worst, lighter_mid, filled, error) -> None:
        self.db.execute(
            "INSERT INTO hedges VALUES (?,?,?,?,?,?,?,?)",
            (time.time(), base, str(size), "sell" if is_sell else "buy", str(worst), str(lighter_mid), str(filled), error),
        )
        self.db.commit()

    def record_quote(self, ts, base, side, price, size, mid, band_bps, interval_s) -> None:
        dist = float(abs(price - mid) / mid * 10_000)
        self.db.execute(
            "INSERT INTO quote_snapshots VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, base, side, str(price), str(size), float(price * size), dist, 1 if dist <= band_bps else 0, interval_s),
        )

    def record_equity(self, ondo_usd, lighter_usd) -> None:
        total = None if ondo_usd is None or lighter_usd is None else ondo_usd + lighter_usd
        self.db.execute(
            "INSERT INTO equity VALUES (?,?,?,?)",
            (time.time(), str(ondo_usd), str(lighter_usd), str(total) if total is not None else None),
        )
        self.db.commit()

    def add_reward(self, week_start: str, week_end: str, usdc: str, points: str, note: str = "") -> None:
        self.db.execute("INSERT INTO rewards VALUES (?,?,?,?,?,?)", (time.time(), week_start, week_end, usdc, points, note))
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
