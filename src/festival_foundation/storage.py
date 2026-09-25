"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS festival_units (
    unit_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    planned_start TEXT NOT NULL,
    planned_end TEXT NOT NULL,
    expected_headcount INTEGER NOT NULL CHECK(expected_headcount > 0),
    required_qualifications_json TEXT NOT NULL,
    required_checks_json TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    status TEXT NOT NULL CHECK(status IN ('draft','scheduled','released','in_progress','paused','finished','cancelled')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS festival_dependencies (
    unit_id TEXT NOT NULL REFERENCES festival_units(unit_id),
    depends_on TEXT NOT NULL REFERENCES festival_units(unit_id),
    PRIMARY KEY (unit_id, depends_on)
);
CREATE TABLE IF NOT EXISTS festival_resources (
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    resource_type TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    maintained_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (site_id, resource_type, resource_key)
);
CREATE TABLE IF NOT EXISTS festival_plans (
    plan_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    status TEXT NOT NULL CHECK(status IN ('draft','released','superseded')),
    resource_versions_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    released_by TEXT,
    released_at TEXT
);
CREATE TABLE IF NOT EXISTS festival_plan_assignments (
    plan_id TEXT NOT NULL REFERENCES festival_plans(plan_id),
    unit_id TEXT NOT NULL REFERENCES festival_units(unit_id),
    unit_version INTEGER NOT NULL CHECK(unit_version >= 1),
    staffing_json TEXT NOT NULL,
    blockers_json TEXT NOT NULL,
    PRIMARY KEY (plan_id, unit_id)
);
CREATE TABLE IF NOT EXISTS festival_sign_offs (
    plan_id TEXT NOT NULL REFERENCES festival_plans(plan_id),
    check_item TEXT NOT NULL,
    responsible_actor TEXT NOT NULL,
    signed_by TEXT,
    signed_at TEXT,
    PRIMARY KEY (plan_id, check_item)
);
CREATE TABLE IF NOT EXISTS festival_capacity_ledger (
    entry_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    plan_id TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    slot_start TEXT NOT NULL,
    slot_end TEXT NOT NULL,
    headcount INTEGER NOT NULL CHECK(headcount > 0),
    state TEXT NOT NULL CHECK(state IN ('reserved','committed','released')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS festival_impacts (
    impact_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    kind TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS festival_impact_assessments (
    impact_id TEXT NOT NULL REFERENCES festival_impacts(impact_id),
    unit_id TEXT NOT NULL REFERENCES festival_units(unit_id),
    disposition TEXT NOT NULL CHECK(disposition IN ('relocate_suggested','manual_handling')),
    suggestion_json TEXT NOT NULL,
    PRIMARY KEY (impact_id, unit_id)
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
