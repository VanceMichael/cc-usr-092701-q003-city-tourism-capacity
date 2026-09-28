"""SQLite 持久化。

并发策略：WAL 模式 + 每个写事务 ``BEGIN IMMEDIATE``，全库写操作在数据库
层面串行化；容量判断与写入在同一事务内完成（先读后写、读即当前提交值），
因此并发售票下不会超卖。等待锁靠 busy_timeout，而不是靠应用层内存计数——
进程重启后容量仍然正确。

崩溃恢复依据：
- refunds.status='pending' 是持久化的待办退款队列；
- reservations.status='waitlisted' 是持久化的候补队列；
两者在服务启动/恢复时由 service.resume_pending() 继续处理，幂等不重。
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS venues (
    venue_id   TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    region     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id        TEXT PRIMARY KEY,
    venue_id          TEXT NOT NULL REFERENCES venues(venue_id),
    title             TEXT NOT NULL,
    activity_type     TEXT NOT NULL,           -- 演出/游园/古建活动/京郊节庆
    start_ts          TEXT NOT NULL,           -- ISO8601
    end_ts            TEXT NOT NULL,
    state             TEXT NOT NULL,           -- scheduled/finished/cancelled
    safe_capacity     INTEGER NOT NULL,        -- 当前安全容量（可被限流下调）
    original_capacity INTEGER NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ticket_types (
    type_id    TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,                  -- paid/free
    price      INTEGER NOT NULL DEFAULT 0,     -- 分为单位
    total_qty  INTEGER NOT NULL,               -- 免费票池初始为 0，凭授权增加
    UNIQUE(session_id, kind, name)
);

CREATE TABLE IF NOT EXISTS teams (
    team_id    TEXT PRIMARY KEY,
    agency_code TEXT NOT NULL,                 -- 归属旅行社，权限隔离依据
    leader_name TEXT NOT NULL,
    size       INTEGER NOT NULL CHECK (size > 0),
    contact    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL REFERENCES sessions(session_id),
    type_id        TEXT NOT NULL REFERENCES ticket_types(type_id),
    team_id        TEXT REFERENCES teams(team_id),   -- NULL = 散客
    visitor_name   TEXT NOT NULL,
    visitor_phone  TEXT NOT NULL DEFAULT '',
    qty            INTEGER NOT NULL CHECK (qty > 0),
    status         TEXT NOT NULL,
    unit_price     INTEGER NOT NULL DEFAULT 0,     -- 下单时单价快照（分）
    booked_by      TEXT NOT NULL DEFAULT '',        -- 创建人 actor_id
    idem_key       TEXT,                       -- 客户端幂等键（中断重试不重复下单）
    reroll_from    TEXT REFERENCES reservations(reservation_id),
    preference     TEXT NOT NULL DEFAULT '{}', -- JSON：转场偏好（区域/活动类型）
    created_at     TEXT NOT NULL,
    confirmed_at   TEXT,
    UNIQUE(idem_key)
);

CREATE TABLE IF NOT EXISTS refunds (
    refund_id      TEXT PRIMARY KEY,
    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
    reason         TEXT NOT NULL,              -- 自愿退/限流/取消
    amount         INTEGER NOT NULL,
    status         TEXT NOT NULL,              -- pending/done/rejected
    rule           TEXT NOT NULL DEFAULT '',
    idem_key       TEXT NOT NULL,
    requested_at   TEXT NOT NULL,
    processed_at   TEXT,
    UNIQUE(reservation_id, reason),            -- 同一预约同一原因只退一次
    UNIQUE(idem_key)
);

CREATE TABLE IF NOT EXISTS compensations (
    comp_id        TEXT PRIMARY KEY,
    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
    session_id     TEXT NOT NULL,
    kind           TEXT NOT NULL,              -- refund/upgrade/voucher
    amount         INTEGER NOT NULL DEFAULT 0,
    note           TEXT NOT NULL DEFAULT '',
    decided_by     TEXT NOT NULL,
    decided_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS authorizations (
    auth_id         TEXT PRIMARY KEY,
    actor_id        TEXT NOT NULL,
    permission      TEXT NOT NULL,             -- free_quota / cross_region_reroll
    scope_session_id TEXT,                     -- NULL = 全局
    payload         TEXT NOT NULL DEFAULT '{}',
    granted_by      TEXT NOT NULL,
    granted_at      TEXT NOT NULL,
    revoked         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS timeline (
    event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    reservation_id TEXT,
    actor_id      TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL,
    rule          TEXT NOT NULL DEFAULT '',
    summary       TEXT NOT NULL,
    payload       TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS checkins (
    checkin_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
    session_id     TEXT NOT NULL,
    checked_by     TEXT NOT NULL,
    ts             TEXT NOT NULL,
    UNIQUE(reservation_id)                     -- 每人只核验一次
);

CREATE INDEX IF NOT EXISTS idx_res_session ON reservations(session_id, status);
CREATE INDEX IF NOT EXISTS idx_res_wait ON reservations(session_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_refunds_status ON refunds(status);
CREATE INDEX IF NOT EXISTS idx_timeline_session ON timeline(session_id, event_id);
"""


class Store:
    """轻量 SQLite 封装：连接管理、事务、行映射。"""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self._conn = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,  # 事务边界自己控制
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=15000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """串行写事务：BEGIN IMMEDIATE 立即拿写锁，容量先读后写在同一事务。

        高并发下同时抢锁的连接可能收到 SQLITE_BUSY/LOCKED，按指数退避
        在应用层重试，保证每个请求都被串行处理而不是失败给调用方。
        """
        conn = self._conn
        delay = 0.005
        last_err: sqlite3.OperationalError | None = None
        for attempt in range(40):
            try:
                conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as e:
                last_err = e
                if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                    raise
                time.sleep(delay)
                delay = min(delay * 1.5, 0.1)
        else:
            raise last_err  # type: ignore[misc]
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        yield self._conn

    # ---- 便捷方法 -------------------------------------------------------
    @staticmethod
    def dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def loads(value: str) -> Any:
        return json.loads(value or "{}")
