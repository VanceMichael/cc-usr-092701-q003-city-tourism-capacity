"""SQLite 存储层。

设计要点：

- 全部写操作在服务层的 *一个* 显式事务中完成（``BEGIN IMMEDIATE``），
  配合进程内互斥锁，保证并发售票下计数与插入的原子性，容量检查与
  占座之间不会插入其它事务。
- 业务状态全部落库：未完成退款为 ``pending``，候补为 ``waiting``，
  服务重启后可据此恢复，不依赖内存队列。
- ``timeline_events`` 与 ``allocation_decisions`` 只追加，分别承载
  场次时间线与逐名额的分配解释。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS venues (
    venue_id  TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    region    TEXT NOT NULL,
    created_ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activities (
    activity_id TEXT PRIMARY KEY,
    title    TEXT NOT NULL,
    kind     TEXT NOT NULL,
    organizer TEXT NOT NULL,
    created_ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    activity_id TEXT NOT NULL REFERENCES activities(activity_id),
    venue_id    TEXT NOT NULL REFERENCES venues(venue_id),
    start_ts    TEXT NOT NULL,
    end_ts      TEXT NOT NULL,
    status      TEXT NOT NULL,              -- scheduled / cancelled / finished
    base_capacity   INTEGER NOT NULL CHECK (base_capacity >= 0),
    safety_capacity INTEGER NOT NULL CHECK (safety_capacity >= 0),
    version     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ticket_types (
    ticket_type_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    name       TEXT NOT NULL,
    price      INTEGER NOT NULL CHECK (price >= 0),   -- 单位：分
    quota      INTEGER,                               -- NULL=仅受场次容量约束
    is_free    INTEGER NOT NULL DEFAULT 0,
    requires_auth INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS teams (
    team_id TEXT PRIMARY KEY,
    agency_id TEXT,                                  -- NULL=散客
    name     TEXT NOT NULL,
    inbound  INTEGER NOT NULL DEFAULT 0,
    contact  TEXT,
    created_ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
    booking_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    team_id    TEXT NOT NULL REFERENCES teams(team_id),
    ticket_type_id TEXT NOT NULL REFERENCES ticket_types(ticket_type_id),
    seats   INTEGER NOT NULL CHECK (seats > 0),
    status  TEXT NOT NULL,
    -- waiting 候补 / confirmed 已确认 / affected 安全调整后待补偿 /
    -- refunded 已退款 / cancelled 已取消 / transferred 已转场
    price_each  INTEGER NOT NULL,
    total_amount INTEGER NOT NULL,
    compensation INTEGER NOT NULL DEFAULT 0,
    original_booking_id TEXT,                        -- 转场产生的新预约指向原预约
    created_ts TEXT NOT NULL,
    updated_ts TEXT NOT NULL,
    version   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_bookings_session_status
    ON bookings(session_id, status);
CREATE INDEX IF NOT EXISTS idx_bookings_waiting
    ON bookings(status, created_ts);
CREATE INDEX IF NOT EXISTS idx_bookings_team ON bookings(team_id);

CREATE TABLE IF NOT EXISTS admissions (
    admission_id TEXT PRIMARY KEY,
    booking_id TEXT NOT NULL REFERENCES bookings(booking_id),
    visitor_name TEXT NOT NULL,
    credential TEXT NOT NULL,
    admitted_ts TEXT NOT NULL,
    admitted_by TEXT NOT NULL,
    UNIQUE(booking_id, credential)
);

CREATE TABLE IF NOT EXISTS refunds (
    refund_id TEXT PRIMARY KEY,
    booking_id TEXT NOT NULL REFERENCES bookings(booking_id),
    amount  INTEGER NOT NULL,
    reason  TEXT NOT NULL,
    status  TEXT NOT NULL,                           -- pending / done
    attempts INTEGER NOT NULL DEFAULT 0,
    created_ts TEXT NOT NULL,
    finished_ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_refunds_pending ON refunds(status);

CREATE TABLE IF NOT EXISTS free_grants (
    grant_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    ticket_type_id TEXT NOT NULL REFERENCES ticket_types(ticket_type_id),
    team_id TEXT REFERENCES teams(team_id),          -- NULL=该场次该票种通用授权
    seats   INTEGER NOT NULL CHECK (seats > 0),
    used    INTEGER NOT NULL DEFAULT 0,
    granted_by TEXT NOT NULL,
    created_ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS timeline_events (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    actor_name TEXT NOT NULL,
    event_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_timeline_session ON timeline_events(session_id, ts);

CREATE TABLE IF NOT EXISTS allocation_decisions (
    decision_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    booking_id TEXT,
    team_id TEXT,
    ts TEXT NOT NULL,
    decision TEXT NOT NULL,
    -- allocated / waitlisted / promoted / rejected / released
    seats INTEGER NOT NULL,
    rule_code TEXT NOT NULL,
    reason TEXT NOT NULL,
    final_arrangement TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_decisions_session ON allocation_decisions(session_id, ts);
"""


class Storage:
    """封装数据库连接与事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self.init_schema()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Cursor]:
        """串行化的写事务：拿锁后立即 ``BEGIN IMMEDIATE``。"""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                yield cur
            except BaseException:
                cur.execute("ROLLBACK")
                raise
            else:
                cur.execute("COMMIT")
            finally:
                cur.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
            finally:
                cur.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
