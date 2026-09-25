"""中秋活动承载与安全放行中枢：编排、容量账本、签署与影响分析。

在基础层（组织、操作者、站点、权限、幂等、审计）之上增加：

- 策划人提交带版本的活动单元与依赖关系，形成可修订的活动方案；
- 结合站点开放窗口、岗位资格、物资/消防检查项与人流上限生成闸门报告；
- 安全员只能签署自己负责的检查项，签名与资源版本摘要绑定；
- 全部签署齐备且资源版本未变化时，方案在一个事务内一次性发布；
- 降雨预警或设施变化只对未开始单元给迁移建议，进行中单元进入人工处置；
- 取消、暂停、恢复、结束遵守显式状态顺序，重复操作结果稳定。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .service import IDENTIFIER, DomainService

ORCHESTRATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS site_resources (
    site_id TEXT PRIMARY KEY REFERENCES sites(site_id),
    max_headcount INTEGER NOT NULL CHECK(max_headcount >= 0),
    indoor INTEGER NOT NULL CHECK(indoor IN (0, 1)),
    facility_status TEXT NOT NULL DEFAULT 'available'
        CHECK(facility_status IN ('available', 'restricted', 'closed')),
    version INTEGER NOT NULL CHECK(version >= 1),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS site_windows (
    window_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    version INTEGER NOT NULL CHECK(version >= 1),
    updated_at TEXT NOT NULL,
    UNIQUE(site_id, window_start)
);
CREATE TABLE IF NOT EXISTS certifications (
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    cert_code TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    version INTEGER NOT NULL CHECK(version >= 1),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(actor_id, cert_code)
);
CREATE TABLE IF NOT EXISTS safety_checks (
    check_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    scope_type TEXT NOT NULL CHECK(scope_type IN ('site', 'unit')),
    scope_key TEXT NOT NULL,
    check_kind TEXT NOT NULL,
    title TEXT NOT NULL,
    owner_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    version INTEGER NOT NULL CHECK(version >= 1),
    updated_at TEXT NOT NULL,
    UNIQUE(scope_type, scope_key, check_kind)
);
CREATE TABLE IF NOT EXISTS event_plans (
    plan_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_revisions (
    plan_id TEXT NOT NULL REFERENCES event_plans(plan_id),
    revision_no INTEGER NOT NULL CHECK(revision_no >= 1),
    status TEXT NOT NULL CHECK(status IN ('drafting', 'published', 'superseded')),
    submitted_by TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT,
    published_by TEXT,
    PRIMARY KEY(plan_id, revision_no)
);
CREATE TABLE IF NOT EXISTS plan_units (
    plan_id TEXT NOT NULL,
    revision_no INTEGER NOT NULL,
    unit_key TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    name TEXT NOT NULL,
    site_id TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    expected_headcount INTEGER NOT NULL CHECK(expected_headcount >= 0),
    posts_json TEXT NOT NULL,
    depends_on_json TEXT NOT NULL,
    rain_backup_site_id TEXT,
    payload_hash TEXT NOT NULL,
    PRIMARY KEY(plan_id, revision_no, unit_key)
);
CREATE TABLE IF NOT EXISTS unit_states (
    plan_id TEXT NOT NULL,
    unit_key TEXT NOT NULL,
    revision_no INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'scheduled', 'in_progress', 'paused', 'manual_handling', 'ended', 'cancelled')),
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(plan_id, unit_key)
);
CREATE TABLE IF NOT EXISTS plan_signatures (
    plan_id TEXT NOT NULL,
    revision_no INTEGER NOT NULL,
    check_id TEXT NOT NULL,
    signer_actor_id TEXT NOT NULL,
    scope_digest TEXT NOT NULL,
    carried INTEGER NOT NULL DEFAULT 0 CHECK(carried IN (0, 1)),
    signed_at TEXT NOT NULL,
    PRIMARY KEY(plan_id, revision_no, check_id)
);
CREATE TABLE IF NOT EXISTS capacity_commits (
    commit_id TEXT NOT NULL PRIMARY KEY,
    plan_id TEXT NOT NULL,
    revision_no INTEGER NOT NULL,
    unit_key TEXT NOT NULL,
    site_id TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    headcount INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(plan_id, unit_key)
);
CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('rain', 'facility')),
    site_id TEXT REFERENCES sites(site_id),
    severity TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    declared_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS relocation_suggestions (
    suggestion_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    plan_id TEXT NOT NULL,
    unit_key TEXT NOT NULL,
    from_site_id TEXT NOT NULL,
    to_site_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending', 'accepted', 'applied', 'discarded')),
    created_at TEXT NOT NULL,
    UNIQUE(incident_id, plan_id, unit_key)
);
"""

TERMINAL_STATES = frozenset({"ended", "cancelled"})
# 每个动作允许的起始状态；重复执行同一动作保持稳定结果。
TRANSITIONS: dict[str, tuple[frozenset[str], str]] = {
    "start": (frozenset({"scheduled"}), "in_progress"),
    "pause": (frozenset({"in_progress"}), "paused"),
    "resume": (frozenset({"paused"}), "in_progress"),
    "end": (frozenset({"in_progress", "paused"}), "ended"),
    "cancel": (frozenset({"scheduled"}), "cancelled"),
}
TRANSITION_ACTIONS = {
    "start": "unit.started",
    "pause": "unit.paused",
    "resume": "unit.resumed",
    "end": "unit.ended",
    "cancel": "unit.cancelled",
}
# 不允许跨级操作时给出的明确顺序提示。
ORDER_HINTS = {
    ("start", "in_progress"): "单元已经开始，请勿重复开始；需要暂停请调用 pause",
    ("start", "paused"): "单元处于暂停状态，请先 resume 再开始",
    ("pause", "scheduled"): "单元尚未开始，开始后才能暂停",
    ("pause", "paused"): "单元已经暂停，请勿重复暂停",
    ("resume", "scheduled"): "单元尚未开始，应调用 start 而不是 resume",
    ("resume", "in_progress"): "单元正在进行，请勿重复恢复",
    ("end", "ended"): "单元已经结束，结果保持不变",
    ("end", "scheduled"): "单元尚未开始；未开始单元请使用 cancel 取消",
    ("cancel", "in_progress"): "单元已经开始，不能取消；请先 end 结束或进入人工处置",
    ("cancel", "cancelled"): "单元已经取消，结果保持不变",
    ("cancel", "ended"): "单元已经结束，不能再取消",
}


class OrchestrationService(DomainService):
    """在基础服务上实现活动编排、容量、签署与影响分析。"""

    def __init__(self, database, clock=None) -> None:
        super().__init__(database, clock)
        database.connection.executescript(ORCHESTRATION_SCHEMA)

    # ------------------------------------------------------------------ 工具

    def _iso(self, value: Any, field: str) -> str:
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是带时区的 ISO 8601 时间") from exc
        if parsed.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _positive_int(self, value: Any, field: str, *, allow_zero: bool = False) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{field} 必须是整数")
        if value < 0 or (value == 0 and not allow_zero):
            raise ValidationError(f"{field} 必须是正整数" if not allow_zero else f"{field} 不能为负")
        return value

    def _site_row(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"场所 {site_id} 不存在")
        return row

    def _org_site(self, connection, actor, site_id: str):
        """加载场所并校验组织边界。"""

        row = self._site_row(connection, site_id)
        if actor.organization_id != row["organization_id"] and actor.role != "admin":
            raise PermissionDenied("不能编排其他组织的场所")
        return row

    def _idem(self, connection, *, request_id: str, action: str, payload: dict[str, Any], create):
        """包装基础幂等：重放时返回首次保存的完整响应。"""

        saved: dict[str, Any] = {}

        def wrapped():
            resource_type, resource_id, response = create()
            saved["response"] = response
            return resource_type, resource_id, response

        receipt = self._idempotent(connection, request_id=request_id, action=action,
                                   payload=payload, create=wrapped)
        if receipt.replayed:
            row = connection.execute(
                "SELECT response_json FROM request_receipts WHERE request_id=?", (request_id,)
            ).fetchone()
            replayed_response = json.loads(row["response_json"])
            # 重放是一次稳定的重复操作：向调用方明确标识为重放。
            if isinstance(replayed_response, dict) and "reapplied" in replayed_response:
                replayed_response["reapplied"] = True
            return replayed_response
        return saved["response"]

    # ------------------------------------------------- 站点资源与开放窗口

    def configure_site_resource(self, *, request_id: str, actor_id: str, site_id: str,
                                 max_headcount: int, indoor: bool,
                                 facility_status: str = "available") -> dict[str, Any]:
        """登记站点人流上限、室内外标记与设施状态，内容变化时版本递增。"""

        payload = {"actor_id": actor_id, "site_id": site_id, "max_headcount": max_headcount,
                   "indoor": indoor, "facility_status": facility_status}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._org_site(connection, actor, site_id)
            max_headcount = self._positive_int(max_headcount, "max_headcount", allow_zero=True)
            indoor_value = 1 if indoor else 0
            if facility_status not in {"available", "restricted", "closed"}:
                raise ValidationError("facility_status 不合法")

            def create():
                existing = connection.execute(
                    "SELECT * FROM site_resources WHERE site_id=?", (site_id,)
                ).fetchone()
                now = self._now()
                if existing is None:
                    version = 1
                    connection.execute(
                        "INSERT INTO site_resources(site_id,max_headcount,indoor,facility_status,version,updated_at)"
                        " VALUES(?,?,?,?,?,?)",
                        (site_id, max_headcount, indoor_value, facility_status, version, now),
                    )
                else:
                    unchanged = (existing["max_headcount"] == max_headcount
                                 and bool(existing["indoor"]) == bool(indoor_value)
                                 and existing["facility_status"] == facility_status)
                    version = existing["version"] if unchanged else existing["version"] + 1
                    connection.execute(
                        "UPDATE site_resources SET max_headcount=?,indoor=?,facility_status=?,version=?,updated_at=?"
                        " WHERE site_id=?",
                        (max_headcount, indoor_value, facility_status, version, now, site_id),
                    )
                append_event(connection, actor_id=actor_id, action="site_resource.configured",
                             resource_type="site", resource_id=site_id,
                             detail={"max_headcount": max_headcount, "indoor": indoor_value,
                                     "facility_status": facility_status, "version": version,
                                     "site_version": site["version"]},
                             occurred_at=self._now())
                response = {"site_id": site_id, "version": version, "max_headcount": max_headcount,
                            "indoor": bool(indoor_value), "facility_status": facility_status}
                return "site_resource", site_id, response

            return self._idem(connection, request_id=request_id, action="configure_site_resource",
                              payload=payload, create=create)

    def upsert_site_window(self, *, request_id: str, actor_id: str, window_id: str, site_id: str,
                           window_start: str, window_end: str, active: bool = True) -> dict[str, Any]:
        """登记站点开放窗口，时间或状态变化时版本递增。"""

        payload = {"actor_id": actor_id, "window_id": window_id, "site_id": site_id,
                   "window_start": window_start, "window_end": window_end, "active": active}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._org_site(connection, actor, site_id)
            window_id = self._identifier(window_id, "window_id")
            start = self._iso(window_start, "window_start")
            end = self._iso(window_end, "window_end")
            if end <= start:
                raise ValidationError("开放窗口结束时间必须晚于开始时间")
            active_value = 1 if active else 0

            def create():
                existing = connection.execute(
                    "SELECT * FROM site_windows WHERE window_id=?", (window_id,)
                ).fetchone()
                now = self._now()
                if existing is not None and existing["site_id"] != site_id:
                    raise ConflictError("窗口编号已经用于其他场所")
                if existing is None:
                    version = 1
                    connection.execute(
                        "INSERT INTO site_windows(window_id,site_id,window_start,window_end,active,version,updated_at)"
                        " VALUES(?,?,?,?,?,?,?)",
                        (window_id, site_id, start, end, active_value, version, now),
                    )
                else:
                    unchanged = (existing["window_start"] == start and existing["window_end"] == end
                                 and bool(existing["active"]) == bool(active_value))
                    version = existing["version"] if unchanged else existing["version"] + 1
                    connection.execute(
                        "UPDATE site_windows SET site_id=?,window_start=?,window_end=?,active=?,version=?,updated_at=?"
                        " WHERE window_id=?",
                        (site_id, start, end, active_value, version, now, window_id),
                    )
                append_event(connection, actor_id=actor_id, action="site_window.upserted",
                             resource_type="site_window", resource_id=window_id,
                             detail={"site_id": site_id, "window_start": start, "window_end": end,
                                     "active": active_value, "version": version},
                             occurred_at=self._now())
                response = {"window_id": window_id, "site_id": site_id, "version": version,
                            "window_start": start, "window_end": end, "active": bool(active_value)}
                return "site_window", window_id, response

            return self._idem(connection, request_id=request_id, action="upsert_site_window",
                              payload=payload, create=create)

    def grant_certification(self, *, request_id: str, actor_id: str, target_actor_id: str,
                            cert_code: str, active: bool = True) -> dict[str, Any]:
        """授予或停用岗位资格，状态变化时版本递增。"""

        payload = {"actor_id": actor_id, "target_actor_id": target_actor_id,
                   "cert_code": cert_code, "active": active}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            target = self._actor(connection, target_actor_id)
            cert_code = self._identifier(cert_code, "cert_code")
            active_value = 1 if active else 0

            def create():
                existing = connection.execute(
                    "SELECT * FROM certifications WHERE actor_id=? AND cert_code=?",
                    (target_actor_id, cert_code),
                ).fetchone()
                now = self._now()
                if existing is None:
                    version = 1
                    connection.execute(
                        "INSERT INTO certifications(actor_id,cert_code,active,version,updated_at)"
                        " VALUES(?,?,?,?,?)",
                        (target_actor_id, cert_code, active_value, version, now),
                    )
                else:
                    unchanged = bool(existing["active"]) == bool(active_value)
                    version = existing["version"] if unchanged else existing["version"] + 1
                    connection.execute(
                        "UPDATE certifications SET active=?,version=?,updated_at=? WHERE actor_id=? AND cert_code=?",
                        (active_value, version, now, target_actor_id, cert_code),
                    )
                append_event(connection, actor_id=actor_id, action="certification.granted",
                             resource_type="actor", resource_id=target_actor_id,
                             detail={"cert_code": cert_code, "active": active_value,
                                     "version": version, "target_organization_id": target.organization_id},
                             occurred_at=self._now())
                response = {"actor_id": target_actor_id, "cert_code": cert_code,
                            "active": bool(active_value), "version": version}
                return "certification", f"{target_actor_id}|{cert_code}", response

            return self._idem(connection, request_id=request_id, action="grant_certification",
                              payload=payload, create=create)

    def assign_safety_check(self, *, request_id: str, actor_id: str, check_id: str,
                            scope_type: str, scope_key: str, check_kind: str, title: str,
                            owner_actor_id: str, active: bool = True) -> dict[str, Any]:
        """登记消防/物资等检查项及其负责安全员；负责人变更或停用时版本递增。"""

        payload = {"actor_id": actor_id, "check_id": check_id, "scope_type": scope_type,
                   "scope_key": scope_key, "check_kind": check_kind, "title": title,
                   "owner_actor_id": owner_actor_id, "active": active}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            check_id = self._identifier(check_id, "check_id")
            if scope_type not in ({"site", "unit"}):
                raise ValidationError("scope_type 只能是 site 或 unit")
            scope_key = self._identifier(scope_key, "scope_key")
            check_kind = self._identifier(check_kind, "check_kind")
            title = self._text(title, "title")
            owner = self._actor(connection, owner_actor_id)
            if owner.role != "reviewer":
                raise ValidationError("检查项负责人必须是安全员（reviewer）")
            if actor.organization_id != owner.organization_id and actor.role != "admin":
                raise PermissionDenied("不能为其他组织的安全员分派检查项")
            if scope_type == "site":
                site = self._site_row(connection, scope_key)
                organization_id = site["organization_id"]
                if actor.organization_id != organization_id and actor.role != "admin":
                    raise PermissionDenied("不能为其他组织的场所登记检查项")
            else:
                organization_id = owner.organization_id
            active_value = 1 if active else 0

            def create():
                existing = connection.execute(
                    "SELECT * FROM safety_checks WHERE check_id=?", (check_id,)
                ).fetchone()
                now = self._now()
                if existing is not None:
                    if (existing["scope_type"], existing["scope_key"], existing["check_kind"]) != (
                        scope_type, scope_key, check_kind
                    ):
                        raise ConflictError("检查项编号的适用范围不能更改")
                    unchanged = (existing["title"] == title and existing["owner_actor_id"] == owner_actor_id
                                 and bool(existing["active"]) == bool(active_value))
                    version = existing["version"] if unchanged else existing["version"] + 1
                    connection.execute(
                        "UPDATE safety_checks SET title=?,owner_actor_id=?,active=?,version=?,updated_at=?"
                        " WHERE check_id=?",
                        (title, owner_actor_id, active_value, version, now, check_id),
                    )
                else:
                    version = 1
                    connection.execute(
                        "INSERT INTO safety_checks(check_id,organization_id,scope_type,scope_key,check_kind,"
                        "title,owner_actor_id,active,version,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (check_id, organization_id, scope_type, scope_key, check_kind, title,
                         owner_actor_id, active_value, version, now),
                    )
                append_event(connection, actor_id=actor_id, action="safety_check.assigned",
                             resource_type="safety_check", resource_id=check_id,
                             detail={"scope_type": scope_type, "scope_key": scope_key,
                                     "check_kind": check_kind, "owner_actor_id": owner_actor_id,
                                     "active": active_value, "version": version},
                             occurred_at=self._now())
                response = {"check_id": check_id, "scope_type": scope_type, "scope_key": scope_key,
                            "check_kind": check_kind, "title": title, "owner_actor_id": owner_actor_id,
                            "active": bool(active_value), "version": version}
                return "safety_check", check_id, response

            return self._idem(connection, request_id=request_id, action="assign_safety_check",
                              payload=payload, create=create)

    # ----------------------------------------------------------- 方案提交

    def _normalize_units(self, connection, actor, units: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(units, list) or not units:
            raise ValidationError("units 必须是非空数组")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw in enumerate(units):
            location = f"units[{index}]"
            if not isinstance(raw, dict):
                raise ValidationError(f"{location} 必须是对象")
            key = self._identifier(raw.get("key", ""), f"{location}.key")
            if key in seen:
                raise ValidationError(f"单元编号 {key} 重复")
            seen.add(key)
            name = self._text(raw.get("name", ""), f"{location}.name")
            site_id = self._identifier(raw.get("site_id", ""), f"{location}.site_id")
            self._org_site(connection, actor, site_id)
            start = self._iso(raw.get("start_time"), f"{location}.start_time")
            end = self._iso(raw.get("end_time"), f"{location}.end_time")
            if end <= start:
                raise ValidationError(f"{location} 结束时间必须晚于开始时间")
            headcount = self._positive_int(raw.get("expected_headcount"), f"{location}.expected_headcount")
            backup = raw.get("rain_backup_site_id")
            if backup:
                backup = self._identifier(backup, f"{location}.rain_backup_site_id")
                self._org_site(connection, actor, backup)
            posts_raw = raw.get("posts", [])
            if not isinstance(posts_raw, list):
                raise ValidationError(f"{location}.posts 必须是数组")
            posts: list[dict[str, str]] = []
            for post_index, post in enumerate(posts_raw):
                if not isinstance(post, dict):
                    raise ValidationError(f"{location}.posts[{post_index}] 必须是对象")
                post_name = self._text(post.get("post", ""), f"{location}.posts[{post_index}].post", 80)
                required_cert = self._identifier(post.get("required_cert", ""),
                                                 f"{location}.posts[{post_index}].required_cert")
                assignee = self._identifier(post.get("actor_id", ""),
                                            f"{location}.posts[{post_index}].actor_id")
                posts.append({"post": post_name, "required_cert": required_cert, "actor_id": assignee})
            depends_raw = raw.get("depends_on", [])
            if not isinstance(depends_raw, list) or not all(isinstance(v, str) for v in depends_raw):
                raise ValidationError(f"{location}.depends_on 必须是字符串数组")
            depends_on = [self._identifier(v, f"{location}.depends_on") for v in depends_raw]
            payload = {"key": key, "name": name, "site_id": site_id, "start_time": start,
                       "end_time": end, "expected_headcount": headcount, "posts": posts,
                       "depends_on": depends_on, "rain_backup_site_id": backup}
            normalized.append(payload)
        # 依赖引用与成环检查。
        keys = {unit["key"] for unit in normalized}
        for unit in normalized:
            for dep in unit["depends_on"]:
                if dep not in keys:
                    raise ValidationError(f"单元 {unit['key']} 依赖了不存在的单元 {dep}")
                if dep == unit["key"]:
                    raise ValidationError(f"单元 {unit['key']} 不能依赖自己")
        visiting: set[str] = set()
        visited: set[str] = set()

        def dfs(node: str) -> None:
            if node in visiting:
                raise ValidationError("活动单元依赖关系中存在环")
            if node in visited:
                return
            visiting.add(node)
            by_key = {u["key"]: u for u in normalized}
            for dep in by_key[node]["depends_on"]:
                dfs(dep)
            visiting.discard(node)
            visited.add(node)

        for unit in normalized:
            dfs(unit["key"])
        return normalized

    def submit_plan(self, *, request_id: str, actor_id: str, plan_id: str, name: str,
                    units: list[dict[str, Any]]) -> dict[str, Any]:
        """提交或更新方案；相对上一修订，内容变化的单元版本递增。"""

        payload = {"actor_id": actor_id, "plan_id": plan_id, "name": name, "units": units}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            plan_id = self._identifier(plan_id, "plan_id")
            name = self._text(name, "name")
            normalized = self._normalize_units(connection, actor, units)
            # 载荷摘要与提交顺序无关，避免同一编排仅因数组顺序不同就产生新版本。
            new_hash = digest(sorted(normalized, key=lambda unit: unit["key"]))

            def create():
                now = self._now()
                plan_row = connection.execute(
                    "SELECT * FROM event_plans WHERE plan_id=?", (plan_id,)
                ).fetchone()
                if plan_row is None:
                    connection.execute(
                        "INSERT INTO event_plans(plan_id,organization_id,name,created_by,created_at)"
                        " VALUES(?,?,?,?,?)",
                        (plan_id, actor.organization_id, name, actor_id, now),
                    )
                    organization_id = actor.organization_id
                else:
                    if plan_row["organization_id"] != actor.organization_id and actor.role != "admin":
                        raise PermissionDenied("不能修改其他组织的方案")
                    organization_id = plan_row["organization_id"]
                    connection.execute("UPDATE event_plans SET name=? WHERE plan_id=?", (name, plan_id))

                revisions = connection.execute(
                    "SELECT * FROM plan_revisions WHERE plan_id=? ORDER BY revision_no", (plan_id,)
                ).fetchall()
                open_draft = next((row for row in reversed(revisions) if row["status"] == "drafting"), None)
                latest = revisions[-1] if revisions else None
                if latest is not None and latest["payload_hash"] == new_hash:
                    # 已发布内容原样重提或草稿内容未变化：稳定返回，不制造新修订、不清空签署。
                    response = {"plan_id": plan_id, "revision_no": latest["revision_no"],
                                "status": latest["status"], "unchanged": True, "units": []}
                    return "event_plan", plan_id, response

                base = open_draft or latest
                if base is not None:
                    base_units = connection.execute(
                        "SELECT * FROM plan_units WHERE plan_id=? AND revision_no=?",
                        (plan_id, base["revision_no"]),
                    ).fetchall()
                else:
                    base_units = []
                base_versions = {row["unit_key"]: row["version"] for row in base_units}
                base_hashes = {row["unit_key"]: row["payload_hash"] for row in base_units}

                if open_draft is not None:
                    revision_no = open_draft["revision_no"]
                    connection.execute(
                        "UPDATE plan_revisions SET submitted_by=?,payload_hash=?,created_at=? WHERE plan_id=? AND revision_no=?",
                        (actor_id, new_hash, now, plan_id, revision_no),
                    )
                    connection.execute(
                        "DELETE FROM plan_units WHERE plan_id=? AND revision_no=?",
                        (plan_id, revision_no),
                    )
                    connection.execute(
                        "DELETE FROM plan_signatures WHERE plan_id=? AND revision_no=?",
                        (plan_id, revision_no),
                    )
                else:
                    revision_no = (latest["revision_no"] if latest else 0) + 1
                    connection.execute(
                        "INSERT INTO plan_revisions(plan_id,revision_no,status,submitted_by,payload_hash,created_at)"
                        " VALUES(?,?, 'drafting',?,?,?)",
                        (plan_id, revision_no, actor_id, new_hash, now),
                    )

                unit_views = []
                for unit in normalized:
                    unit_hash = digest({k: v for k, v in unit.items() if k != "key"})
                    if unit["key"] in base_hashes and base_hashes[unit["key"]] == unit_hash:
                        version = base_versions[unit["key"]]
                    else:
                        version = base_versions.get(unit["key"], 0) + 1
                    connection.execute(
                        "INSERT INTO plan_units(plan_id,revision_no,unit_key,version,name,site_id,start_time,"
                        "end_time,expected_headcount,posts_json,depends_on_json,rain_backup_site_id,payload_hash)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (plan_id, revision_no, unit["key"], version, unit["name"], unit["site_id"],
                         unit["start_time"], unit["end_time"], unit["expected_headcount"],
                         canonical_json(unit["posts"]), canonical_json(unit["depends_on"]),
                         unit["rain_backup_site_id"], unit_hash),
                    )
                    unit_views.append({"key": unit["key"], "version": version, "site_id": unit["site_id"],
                                       "changed": version != base_versions.get(unit["key"])})
                append_event(connection, actor_id=actor_id, action="plan.submitted",
                             resource_type="event_plan", resource_id=plan_id,
                             detail={"revision_no": revision_no, "units": unit_views,
                                     "payload_hash": new_hash},
                             occurred_at=now)
                response = {"plan_id": plan_id, "revision_no": revision_no, "status": "drafting",
                            "unchanged": False, "units": unit_views}
                return "event_plan", plan_id, response

            return self._idem(connection, request_id=request_id, action="submit_plan",
                              payload=payload, create=create)

    def discard_draft(self, *, request_id: str, actor_id: str, plan_id: str) -> dict[str, Any]:
        """放弃尚未发布的修订（例如拒绝迁移建议后）。"""

        payload = {"actor_id": actor_id, "plan_id": plan_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            plan = connection.execute("SELECT * FROM event_plans WHERE plan_id=?", (plan_id,)).fetchone()
            if plan is None:
                raise NotFoundError("方案不存在")

            def create():
                draft = connection.execute(
                    "SELECT * FROM plan_revisions WHERE plan_id=? AND status='drafting'"
                    " ORDER BY revision_no DESC LIMIT 1",
                    (plan_id,),
                ).fetchone()
                if draft is None:
                    raise ConflictError("方案没有待发布的修订")
                connection.execute(
                    "UPDATE relocation_suggestions SET status='pending' WHERE plan_id=? AND status='accepted'",
                    (plan_id,),
                )
                connection.execute(
                    "DELETE FROM plan_signatures WHERE plan_id=? AND revision_no=?",
                    (plan_id, draft["revision_no"]),
                )
                connection.execute(
                    "DELETE FROM plan_units WHERE plan_id=? AND revision_no=?",
                    (plan_id, draft["revision_no"]),
                )
                connection.execute("DELETE FROM plan_revisions WHERE plan_id=? AND revision_no=?",
                                   (plan_id, draft["revision_no"]))
                append_event(connection, actor_id=actor_id, action="plan.draft_discarded",
                             resource_type="event_plan", resource_id=plan_id,
                             detail={"revision_no": draft["revision_no"]}, occurred_at=self._now())
                return "event_plan", plan_id, {"plan_id": plan_id,
                                                "discarded_revision": draft["revision_no"]}

            return self._idem(connection, request_id=request_id, action="discard_draft",
                              payload=payload, create=create)

    # ------------------------------------------------------------- 闸门评估

    def _load_revision(self, connection, plan_id: str, *, status: str | None = None,
                       latest: bool = True):
        query = "SELECT * FROM plan_revisions WHERE plan_id=?"
        parameters: list[Any] = [plan_id]
        if status:
            query += " AND status=?"
            parameters.append(status)
        query += " ORDER BY revision_no" + (" DESC LIMIT 1" if latest else "")
        rows = connection.execute(query, parameters).fetchall()
        if latest:
            return rows[0] if rows else None
        return rows

    def _unit_rows(self, connection, plan_id: str, revision_no: int):
        return connection.execute(
            "SELECT * FROM plan_units WHERE plan_id=? AND revision_no=? ORDER BY unit_key",
            (plan_id, revision_no),
        ).fetchall()

    def _state_map(self, connection, plan_id: str) -> dict[str, str]:
        return {row["unit_key"]: row for row in connection.execute(
            "SELECT * FROM unit_states WHERE plan_id=?", (plan_id,)).fetchall()}

    def _checks_for(self, connection, organization_id: str):
        return connection.execute(
            "SELECT * FROM safety_checks WHERE organization_id=? AND active=1", (organization_id,)
        ).fetchall()

    def _site_scope_digest(self, connection, site_id: str) -> str:
        resource = connection.execute("SELECT * FROM site_resources WHERE site_id=?", (site_id,)).fetchone()
        site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        windows = connection.execute(
            "SELECT window_id,version,window_start,window_end,active FROM site_windows"
            " WHERE site_id=? ORDER BY window_id", (site_id,)
        ).fetchall()
        material = {
            "site_id": site_id,
            "site_version": site["version"] if site else None,
            "resource": None if resource is None else {
                "version": resource["version"], "max_headcount": resource["max_headcount"],
                "indoor": resource["indoor"], "facility_status": resource["facility_status"],
            },
            "windows": [{"window_id": w["window_id"], "version": w["version"],
                         "window_start": w["window_start"], "window_end": w["window_end"],
                         "active": w["active"]} for w in windows],
        }
        return digest(material)

    def _unit_scope_digest(self, unit_row) -> str:
        posts = sorted(json.loads(unit_row["posts_json"]), key=lambda p: (p["post"], p["actor_id"]))
        return digest({"unit_key": unit_row["unit_key"], "version": unit_row["version"],
                       "payload_hash": unit_row["payload_hash"]})

    def _evaluate(self, connection, plan_row, revision_row, unit_rows: list, *,
                  ledger_override: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
        """计算一个修订的完整闸门报告。"""

        plan_id = plan_row["plan_id"]
        organization_id = plan_row["organization_id"]
        revision_no = revision_row["revision_no"]
        states = self._state_map(connection, plan_id)
        checks = self._checks_for(connection, organization_id)

        unit_blockers: dict[str, list[dict[str, str]]] = {u["unit_key"]: [] for u in unit_rows}
        unit_by_key = {u["unit_key"]: u for u in unit_rows}

        resources = {}
        windows_by_site: dict[str, list] = {}
        sites_used = sorted({u["site_id"] for u in unit_rows})
        for site_id in sites_used:
            resources[site_id] = connection.execute(
                "SELECT * FROM site_resources WHERE site_id=?", (site_id,)).fetchone()
            windows_by_site[site_id] = connection.execute(
                "SELECT * FROM site_windows WHERE site_id=? AND active=1 ORDER BY window_start",
                (site_id,)).fetchall()

        for unit in unit_rows:
            key = unit["unit_key"]
            blockers = unit_blockers[key]
            resource = resources[unit["site_id"]]
            if resource is None:
                blockers.append({"code": "site_not_configured",
                                 "message": f"场所 {unit['site_id']} 尚未登记人流上限与设施条件"})
            else:
                if resource["facility_status"] == "closed":
                    blockers.append({"code": "site_closed",
                                     "message": f"场所 {unit['site_id']} 已关闭，不能安排活动"})
                elif resource["facility_status"] == "restricted":
                    blockers.append({"code": "site_restricted",
                                     "message": f"场所 {unit['site_id']} 设施受限，需要迁移或人工确认"})
                if unit["expected_headcount"] > resource["max_headcount"]:
                    blockers.append({"code": "site_headcount_exceeded",
                                     "message": f"单元预计 {unit['expected_headcount']} 人，超过场所上限 "
                                                f"{resource['max_headcount']} 人"})
            windows = windows_by_site[unit["site_id"]]
            covering = [w for w in windows
                        if w["window_start"] <= unit["start_time"] and unit["end_time"] <= w["window_end"]]
            if not covering:
                blockers.append({"code": "outside_open_window",
                                 "message": f"单元时间 {unit['start_time']}~{unit['end_time']} "
                                            f"不在场所 {unit['site_id']} 的开放窗口内"})
            for post in json.loads(unit["posts_json"]):
                assignee = connection.execute(
                    "SELECT * FROM actors WHERE actor_id=?", (post["actor_id"],)).fetchone()
                if assignee is None or not assignee["active"]:
                    blockers.append({"code": "assignee_unavailable",
                                     "message": f"岗位 {post['post']} 的操作者 {post['actor_id']} 不存在或已停用"})
                    continue
                if assignee["organization_id"] != organization_id and assignee["role"] != "admin":
                    blockers.append({"code": "assignee_organization_mismatch",
                                     "message": f"岗位 {post['post']} 的操作者不属于本组织"})
                cert = connection.execute(
                    "SELECT * FROM certifications WHERE actor_id=? AND cert_code=?",
                    (post["actor_id"], post["required_cert"]),
                ).fetchone()
                if cert is None or not cert["active"]:
                    blockers.append({"code": "missing_certification",
                                     "message": f"岗位 {post['post']} 需要资格 {post['required_cert']}，"
                                                f"{post['actor_id']} 尚未持有有效资格"})
            for dep_key in json.loads(unit["depends_on_json"]):
                dep = unit_by_key[dep_key]
                if dep["end_time"] > unit["start_time"]:
                    blockers.append({"code": "dependency_time_order",
                                     "message": f"依赖单元 {dep_key} 结束晚于本单元开始，执行顺序冲突"})

        # 容量账本：其他方案已发布承诺 + 本修订的投影（终态单元不再占位）。
        projected: dict[str, list[dict[str, Any]]] = {site_id: [] for site_id in sites_used}
        if ledger_override is not None:
            for site_id, entries in ledger_override.items():
                projected.setdefault(site_id, []).extend(entries)
        else:
            for site_id in sites_used:
                rows = connection.execute(
                    "SELECT c.*, s.state FROM capacity_commits c JOIN unit_states s"
                    " ON c.plan_id=s.plan_id AND c.unit_key=s.unit_key"
                    " WHERE c.site_id=? AND s.state NOT IN ('ended','cancelled')",
                    (site_id,),
                ).fetchall()
                for row in rows:
                    if row["plan_id"] == plan_id:
                        continue  # 本方案承诺以修订投影替换
                    projected[site_id].append(
                        {"start_time": row["start_time"], "end_time": row["end_time"],
                         "headcount": row["headcount"], "plan_id": row["plan_id"],
                         "unit_key": row["unit_key"]})
            for unit in unit_rows:
                state_row = states.get(unit["unit_key"])
                state = state_row["state"] if state_row else "scheduled"
                if state in TERMINAL_STATES:
                    continue
                projected[unit["site_id"]].append(
                    {"start_time": unit["start_time"], "end_time": unit["end_time"],
                     "headcount": unit["expected_headcount"], "plan_id": plan_id,
                     "unit_key": unit["unit_key"]})

        plan_blockers: list[dict[str, Any]] = []
        for site_id in sites_used:
            resource = resources[site_id]
            if resource is None:
                continue
            entries = projected.get(site_id, [])
            reported: set[tuple] = set()
            for entry in entries:
                overlap = [e for e in entries
                           if e["start_time"] < entry["end_time"] and e["end_time"] > entry["start_time"]]
                peak = sum(e["headcount"] for e in overlap)
                cluster = tuple(sorted(f"{e['plan_id']}/{e['unit_key']}" for e in overlap))
                if peak > resource["max_headcount"] and cluster not in reported:
                    reported.add(cluster)
                    involved = list(cluster)
                    message = (f"场所 {site_id} 时间重叠人流峰值 {peak} 超过上限 "
                               f"{resource['max_headcount']}")
                    plan_blockers.append({"code": "site_capacity_exceeded", "site_id": site_id,
                                          "peak": peak, "cap": resource["max_headcount"],
                                          "involved": involved, "message": message})
                    if entry["plan_id"] == plan_id:
                        unit_blockers[entry["unit_key"]].append(
                            {"code": "site_capacity_exceeded", "message": message})

        # 必需检查项：每个使用场地必须有消防检查；单元检查按登记结果加入。
        required: list[dict[str, Any]] = []
        prior_published = connection.execute(
            "SELECT revision_no FROM plan_revisions WHERE plan_id=? AND status='published'"
            " ORDER BY revision_no DESC LIMIT 1", (plan_id,)).fetchone()
        for site_id in sites_used:
            site_checks = [c for c in checks if c["scope_type"] == "site" and c["scope_key"] == site_id]
            fire = next((c for c in site_checks if c["check_kind"] == "fire"), None)
            if fire is None:
                plan_blockers.append({"code": "missing_check_assignment", "site_id": site_id,
                                      "message": f"场所 {site_id} 缺少负责的消防检查项，无人可以签署"})
                required.append({"scope_type": "site", "scope_key": site_id, "check_kind": "fire",
                                 "check_id": None, "title": "消防安全检查", "owner_actor_id": None,
                                 "scope_digest": self._site_scope_digest(connection, site_id),
                                 "site_id": site_id, "unit_key": None})
            else:
                required.append(self._check_view(connection, plan_id, revision_no, prior_published,
                                                 fire, site_id=site_id))
        for unit in unit_rows:
            for check in checks:
                if check["scope_type"] == "unit" and check["scope_key"] == unit["unit_key"]:
                    required.append(self._check_view(connection, plan_id, revision_no, prior_published,
                                                     check, unit_row=unit))

        for item in required:
            if item["check_id"] is None:
                continue
            if item["status"] in ("missing", "stale"):
                plan_blockers.append({"code": f"signature_{item['status']}", "check_id": item["check_id"],
                                      "scope": f"{item['scope_type']}:{item['scope_key']}",
                                      "owner_actor_id": item["owner_actor_id"],
                                      "message": f"检查项 {item['title']} 尚{'未' if item['status'] == 'missing' else '因资源版本变化而'}签署"})

        # 草稿相对已发布版本：已经开始（或已终态）的单元不允许被调整，必须人工处置。
        if revision_row["status"] == "drafting":
            prior_published_row = connection.execute(
                "SELECT revision_no FROM plan_revisions WHERE plan_id=? AND status='published'"
                " ORDER BY revision_no DESC LIMIT 1", (plan_id,)).fetchone()
            if prior_published_row is not None:
                prior_units = {u["unit_key"]: u for u in
                               self._unit_rows(connection, plan_id, prior_published_row["revision_no"])}
                draft_keys = {u["unit_key"] for u in unit_rows}
                for unit in unit_rows:
                    previous = prior_units.get(unit["unit_key"])
                    state_row = states.get(unit["unit_key"])
                    if (previous is not None and state_row is not None
                            and state_row["state"] != "scheduled"
                            and (previous["site_id"] != unit["site_id"]
                                 or previous["payload_hash"] != unit["payload_hash"])):
                        message = (f"单元 {unit['unit_key']} 已经开始（{state_row['state']}），"
                                   f"资源调整只能作用于未开始单元，当前须人工处置")
                        plan_blockers.append({"code": "live_unit_modified",
                                              "unit_key": unit["unit_key"],
                                              "state": state_row["state"], "message": message})
                        unit_blockers[unit["unit_key"]].append(
                            {"code": "live_unit_modified", "message": message})
                for key, previous in prior_units.items():
                    if key in draft_keys:
                        continue
                    state_row = states.get(key)
                    if state_row is not None and state_row["state"] != "scheduled":
                        plan_blockers.append({
                            "code": "live_unit_removed", "unit_key": key,
                            "state": state_row["state"],
                            "message": f"单元 {key} 已经开始（{state_row['state']}），不能在新修订中静默移除"})

        unit_views = {}
        for unit in unit_rows:
            key = unit["unit_key"]
            state_row = states.get(key)
            unit_views[key] = {
                "version": unit["version"], "name": unit["name"], "site_id": unit["site_id"],
                "start_time": unit["start_time"], "end_time": unit["end_time"],
                "expected_headcount": unit["expected_headcount"],
                "posts": json.loads(unit["posts_json"]),
                "depends_on": json.loads(unit["depends_on_json"]),
                "rain_backup_site_id": unit["rain_backup_site_id"],
                "state": state_row["state"] if state_row else "proposed",
                "blockers": unit_blockers[key],
                "releasable": not unit_blockers[key],
            }

        scope_material = {item["check_id"] or f"mandatory:{item['scope_type']}:{item['scope_key']}":
                          item["scope_digest"] for item in required}
        scope_material["revision_payload"] = revision_row["payload_hash"]
        resource_digest = digest(scope_material)
        releasable = (revision_row["status"] == "drafting"
                      and not plan_blockers
                      and all(item["status"] in ("signed", "carried") for item in required if item["check_id"] is not None)
                      and all(v["releasable"] for v in unit_views.values()))
        return {
            "plan_id": plan_id,
            "name": plan_row["name"],
            "revision_no": revision_no,
            "status": revision_row["status"],
            "resource_digest": resource_digest,
            "releasable": releasable,
            "plan_blockers": plan_blockers,
            "required_checks": required,
            "units": unit_views,
            "submitted_by": revision_row["submitted_by"],
            "published_by": revision_row["published_by"],
            "published_at": revision_row["published_at"],
        }

    def _check_view(self, connection, plan_id: str, revision_no: int, prior_published,
                    check_row, *, site_id: str | None = None, unit_row=None) -> dict[str, Any]:
        if check_row["scope_type"] == "site":
            scope_digest = self._site_scope_digest(connection, check_row["scope_key"])
            unit_key = None
        else:
            scope_digest = self._unit_scope_digest(unit_row)
            unit_key = unit_row["unit_key"]
        signature = connection.execute(
            "SELECT * FROM plan_signatures WHERE plan_id=? AND revision_no=? AND check_id=?",
            (plan_id, revision_no, check_row["check_id"]),
        ).fetchone()
        status = "missing"
        signed_by = None
        signed_at = None
        carried = False
        if signature is not None:
            carried = bool(signature["carried"])
            if signature["scope_digest"] == scope_digest:
                status = "carried" if carried else "signed"
                signed_by = signature["signer_actor_id"]
                signed_at = signature["signed_at"]
            else:
                status = "stale"
                signed_by = signature["signer_actor_id"]
                signed_at = signature["signed_at"]
        elif prior_published is not None:
            previous = connection.execute(
                "SELECT * FROM plan_signatures WHERE plan_id=? AND revision_no=? AND check_id=?",
                (plan_id, prior_published["revision_no"], check_row["check_id"]),
            ).fetchone()
            if previous is not None and previous["scope_digest"] == scope_digest:
                status = "carried"
                signed_by = previous["signer_actor_id"]
                signed_at = previous["signed_at"]
                carried = True
            elif previous is not None:
                # 资源版本在旧签署之后发生变化，旧签失效需重签。
                status = "stale"
                signed_by = previous["signer_actor_id"]
                signed_at = previous["signed_at"]
        return {"check_id": check_row["check_id"], "scope_type": check_row["scope_type"],
                "scope_key": check_row["scope_key"], "check_kind": check_row["check_kind"],
                "title": check_row["title"], "owner_actor_id": check_row["owner_actor_id"],
                "status": status, "signed_by": signed_by, "signed_at": signed_at,
                "carried": carried, "scope_digest": scope_digest,
                "site_id": site_id, "unit_key": unit_key}

    def get_plan_gate(self, plan_id: str) -> dict[str, Any]:
        """返回方案最新修订的闸门：能否放行、被什么条件阻挡。"""

        connection = self.database.connection
        plan_row = connection.execute("SELECT * FROM event_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if plan_row is None:
            raise NotFoundError("方案不存在")
        revision = self._load_revision(connection, plan_id)
        if revision is None:
            raise NotFoundError("方案没有任何修订")
        units = self._unit_rows(connection, plan_id, revision["revision_no"])
        return self._evaluate(connection, plan_row, revision, units)

    def release_board(self, site_id: str | None = None) -> dict[str, Any]:
        """跨方案展示每个单元的放行结论与阻挡条件。

        - 待发布修订：releasable 表示该修订是否具备一次性发布条件；
        - 已发布修订：released 表示单元当前可现场放行（未处于取消/人工处置等状态）；
        - blockers 始终列出具体阻挡条件，便于回答“被什么条件挡住”。
        """

        connection = self.database.connection
        items: list[dict[str, Any]] = []
        plans = connection.execute("SELECT * FROM event_plans ORDER BY plan_id").fetchall()
        for plan_row in plans:
            revision = self._load_revision(connection, plan_row["plan_id"])
            if revision is None:
                continue
            gate = self._evaluate(connection, plan_row, revision,
                                  self._unit_rows(connection, plan_row["plan_id"], revision["revision_no"]))
            for key, unit in gate["units"].items():
                if site_id and unit["site_id"] != site_id:
                    continue
                blockers = [b["message"] for b in unit["blockers"]]
                if revision["status"] == "drafting":
                    blockers.extend(b["message"] for b in gate["plan_blockers"])
                    releasable = gate["releasable"] and unit["releasable"]
                    released = False
                else:
                    releasable = False
                    released = unit["state"] in ("scheduled", "in_progress", "paused")
                    if unit["state"] == "manual_handling":
                        blockers.append("单元处于人工处置状态，等待现场决策")
                    elif unit["state"] == "cancelled":
                        blockers.append("单元已取消")
                items.append({"plan_id": plan_row["plan_id"], "revision_no": gate["revision_no"],
                              "revision_status": gate["status"], "unit_key": key,
                              "state": unit["state"], "site_id": unit["site_id"],
                              "releasable": releasable, "released": released,
                              "blockers": blockers})
        return {"items": items}

    def capacity_ledger(self, site_id: str) -> dict[str, Any]:
        """查看场地的容量配置与已发布承诺。"""

        connection = self.database.connection
        self._site_row(connection, site_id)
        resource = connection.execute("SELECT * FROM site_resources WHERE site_id=?", (site_id,)).fetchone()
        commits = []
        for row in connection.execute(
            "SELECT c.*, s.state FROM capacity_commits c JOIN unit_states s"
            " ON c.plan_id=s.plan_id AND c.unit_key=s.unit_key WHERE c.site_id=? ORDER BY c.start_time",
            (site_id,),
        ):
            commits.append({"plan_id": row["plan_id"], "unit_key": row["unit_key"],
                            "revision_no": row["revision_no"], "site_id": row["site_id"],
                            "start_time": row["start_time"], "end_time": row["end_time"],
                            "headcount": row["headcount"], "state": row["state"]})
        return {"site_id": site_id,
                "max_headcount": None if resource is None else resource["max_headcount"],
                "version": None if resource is None else resource["version"],
                "commits": commits}

    # ----------------------------------------------------------- 签署发布

    def sign_check(self, *, request_id: str, actor_id: str, plan_id: str, check_id: str) -> dict[str, Any]:
        """安全员对自己负责的检查项签署；资源版本变化后旧签署变为 stale，需重新签署。"""

        payload = {"actor_id": actor_id, "plan_id": plan_id, "check_id": check_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            plan_row = connection.execute("SELECT * FROM event_plans WHERE plan_id=?", (plan_id,)).fetchone()
            if plan_row is None:
                raise NotFoundError("方案不存在")

            def create():
                revision = self._load_revision(connection, plan_id, status="drafting")
                if revision is None:
                    raise ConflictError("方案没有待签署的修订")
                units = self._unit_rows(connection, plan_id, revision["revision_no"])
                gate = self._evaluate(connection, plan_row, revision, units)
                target = next((c for c in gate["required_checks"] if c["check_id"] == check_id), None)
                if target is None:
                    raise NotFoundError("该检查项不属于本修订的必需签署范围")
                if target["owner_actor_id"] != actor_id:
                    raise PermissionDenied("安全员只能签署自己负责的检查项")
                now = self._now()
                existing = connection.execute(
                    "SELECT * FROM plan_signatures WHERE plan_id=? AND revision_no=? AND check_id=?",
                    (plan_id, revision["revision_no"], check_id),
                ).fetchone()
                if existing is not None and existing["scope_digest"] == target["scope_digest"]:
                    response = {"plan_id": plan_id, "revision_no": revision["revision_no"],
                                "check_id": check_id, "status": "signed", "reapplied": True}
                    return "plan_signature", f"{plan_id}:{check_id}", response
                connection.execute(
                    "INSERT INTO plan_signatures(plan_id,revision_no,check_id,signer_actor_id,"
                    "scope_digest,carried,signed_at) VALUES(?,?,?,?,?,0,?) "
                    "ON CONFLICT(plan_id,revision_no,check_id) DO UPDATE SET signer_actor_id=excluded.signer_actor_id,"
                    "scope_digest=excluded.scope_digest,carried=0,signed_at=excluded.signed_at",
                    (plan_id, revision["revision_no"], check_id, actor_id,
                     target["scope_digest"], now),
                )
                append_event(connection, actor_id=actor_id, action="plan.check_signed",
                             resource_type="event_plan", resource_id=plan_id,
                             detail={"revision_no": revision["revision_no"], "check_id": check_id,
                                     "scope_type": target["scope_type"], "scope_key": target["scope_key"],
                                     "scope_digest": target["scope_digest"],
                                     "resource_digest": gate["resource_digest"]},
                             occurred_at=now)
                response = {"plan_id": plan_id, "revision_no": revision["revision_no"],
                            "check_id": check_id, "status": "signed", "reapplied": False}
                return "plan_signature", f"{plan_id}:{check_id}", response

            return self._idem(connection, request_id=request_id, action="sign_check",
                              payload=payload, create=create)

    def publish_plan(self, *, request_id: str, actor_id: str, plan_id: str) -> dict[str, Any]:
        """所有必需签署齐备且资源版本未变化时，在单个事务内一次性发布。"""

        payload = {"actor_id": actor_id, "plan_id": plan_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            plan_row = connection.execute("SELECT * FROM event_plans WHERE plan_id=?", (plan_id,)).fetchone()
            if plan_row is None:
                raise NotFoundError("方案不存在")

            def create():
                revision = self._load_revision(connection, plan_id, status="drafting")
                if revision is None:
                    raise ConflictError("方案没有待发布的修订")
                units = self._unit_rows(connection, plan_id, revision["revision_no"])
                gate = self._evaluate(connection, plan_row, revision, units)
                if not gate["releasable"]:
                    codes = sorted({b["code"] for b in gate["plan_blockers"]}
                                   | {b["code"] for u in gate["units"].values() for b in u["blockers"]}
                                   | {f"signature_{c['status']}" for c in gate["required_checks"]
                                      if c["check_id"] and c["status"] not in ("signed", "carried")})
                    raise ConflictError("方案尚不具备放行条件：" + "、".join(codes))
                prior = connection.execute(
                    "SELECT * FROM plan_revisions WHERE plan_id=? AND status='published'"
                    " ORDER BY revision_no DESC LIMIT 1", (plan_id,)).fetchone()
                prior_units = {u["unit_key"]: u for u in
                               (self._unit_rows(connection, plan_id, prior["revision_no"]) if prior else [])}
                states = self._state_map(connection, plan_id)
                now = self._now()

                # 变化的单元必须仍未开始；删除的单元若已开始则拒绝发布——不能静默换场。
                for unit in units:
                    previous = prior_units.get(unit["unit_key"])
                    state_row = states.get(unit["unit_key"])
                    if previous is not None and (previous["site_id"] != unit["site_id"]
                                                 or previous["payload_hash"] != unit["payload_hash"]):
                        if state_row is None or state_row["state"] != "scheduled":
                            state = state_row["state"] if state_row else "proposed"
                            raise ConflictError(
                                f"单元 {unit['unit_key']} 当前状态为 {state}，资源调整只允许作用于未开始单元")
                for key, previous in prior_units.items():
                    if key not in {u["unit_key"] for u in units}:
                        state_row = states.get(key)
                        if state_row is None or state_row["state"] != "scheduled":
                            raise ConflictError(f"单元 {key} 已开始，不能在新修订中移除")
                        connection.execute(
                            "UPDATE unit_states SET state='cancelled',updated_by=?,updated_at=?"
                            " WHERE plan_id=? AND unit_key=?",
                            (actor_id, now, plan_id, key),
                        )
                        append_event(connection, actor_id=actor_id, action="unit.cancelled",
                                     resource_type="event_plan", resource_id=plan_id,
                                     detail={"unit_key": key, "reason": "removed_in_revision",
                                             "revision_no": revision["revision_no"]},
                                     occurred_at=now)

                # 沿用未受影响范围的签署，保证责任链完整。
                for item in gate["required_checks"]:
                    if item["status"] != "carried" or item["check_id"] is None:
                        continue
                    exists = connection.execute(
                        "SELECT 1 FROM plan_signatures WHERE plan_id=? AND revision_no=? AND check_id=?",
                        (plan_id, revision["revision_no"], item["check_id"]),
                    ).fetchone()
                    if exists is None:
                        connection.execute(
                            "INSERT INTO plan_signatures(plan_id,revision_no,check_id,signer_actor_id,"
                            "scope_digest,carried,signed_at) VALUES(?,?,?,?,?,1,?)",
                            (plan_id, revision["revision_no"], item["check_id"], item["signed_by"],
                             item["scope_digest"], item["signed_at"]),
                        )

                # 重建本方案容量承诺。
                connection.execute("DELETE FROM capacity_commits WHERE plan_id=?", (plan_id,))
                for unit in units:
                    state_row = states.get(unit["unit_key"])
                    state = state_row["state"] if state_row else "scheduled"
                    if state in TERMINAL_STATES:
                        continue
                    connection.execute(
                        "INSERT INTO capacity_commits(commit_id,plan_id,revision_no,unit_key,site_id,"
                        "start_time,end_time,headcount,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, plan_id, revision["revision_no"], unit["unit_key"],
                         unit["site_id"], unit["start_time"], unit["end_time"],
                         unit["expected_headcount"], now),
                    )
                    if state_row is None:
                        connection.execute(
                            "INSERT INTO unit_states(plan_id,unit_key,revision_no,state,updated_by,updated_at)"
                            " VALUES(?,?,?,?,?,?)",
                            (plan_id, unit["unit_key"], revision["revision_no"], "scheduled", actor_id, now),
                        )
                    elif state_row["revision_no"] != revision["revision_no"]:
                        connection.execute(
                            "UPDATE unit_states SET revision_no=? WHERE plan_id=? AND unit_key=?",
                            (revision["revision_no"], plan_id, unit["unit_key"]),
                        )

                connection.execute(
                    "UPDATE plan_revisions SET status='superseded' WHERE plan_id=? AND status='published'",
                    (plan_id,),
                )
                connection.execute(
                    "UPDATE plan_revisions SET status='published',published_at=?,published_by=?"
                    " WHERE plan_id=? AND revision_no=?",
                    (now, actor_id, plan_id, revision["revision_no"]),
                )
                connection.execute(
                    "UPDATE relocation_suggestions SET status='applied' WHERE plan_id=? AND status='accepted'",
                    (plan_id,),
                )
                append_event(connection, actor_id=actor_id, action="plan.published",
                             resource_type="event_plan", resource_id=plan_id,
                             detail={"revision_no": revision["revision_no"],
                                     "resource_digest": gate["resource_digest"],
                                     "units": list(gate["units"].keys()),
                                     "signatures": [{"check_id": c["check_id"],
                                                     "signed_by": c["signed_by"], "carried": c["carried"]}
                                                    for c in gate["required_checks"] if c["check_id"]]},
                             occurred_at=now)
                fresh = self._evaluate(connection, plan_row,
                                       self._load_revision(connection, plan_id, status="published"),
                                       self._unit_rows(connection, plan_id, revision["revision_no"]))
                response = {"plan_id": plan_id, "revision_no": revision["revision_no"],
                            "status": "published", "gate": fresh}
                return "event_plan", plan_id, response

            return self._idem(connection, request_id=request_id, action="publish_plan",
                              payload=payload, create=create)

    # --------------------------------------------------- 降雨与设施影响分析

    def _apply_incident_impact(self, connection, incident_row) -> dict[str, Any]:
        """对已发布方案执行影响分析并落库：进行中→人工处置；未开始→迁移建议。"""

        manual_units: list[dict[str, str]] = []
        suggestions: list[dict[str, Any]] = []
        kind = incident_row["kind"]
        incident_site = incident_row["site_id"]
        plans = connection.execute("SELECT * FROM event_plans ORDER BY plan_id").fetchall()
        for plan_row in plans:
            revision = self._load_revision(connection, plan_row["plan_id"], status="published")
            if revision is None:
                continue
            units = self._unit_rows(connection, plan_row["plan_id"], revision["revision_no"])
            states = self._state_map(connection, plan_row["plan_id"])
            for unit in units:
                affected = False
                if kind == "facility":
                    affected = unit["site_id"] == incident_site
                else:
                    resource = connection.execute(
                        "SELECT * FROM site_resources WHERE site_id=?", (unit["site_id"],)).fetchone()
                    affected = resource is None or not resource["indoor"]
                if not affected:
                    continue
                state_row = states.get(unit["unit_key"])
                state = state_row["state"] if state_row else "scheduled"
                if state in ("in_progress", "paused"):
                    if state != "manual_handling":
                        connection.execute(
                            "UPDATE unit_states SET state='manual_handling',updated_by=?,updated_at=?"
                            " WHERE plan_id=? AND unit_key=?",
                            (incident_row["declared_by"], self._now(),
                             plan_row["plan_id"], unit["unit_key"]),
                        )
                        append_event(connection, actor_id=incident_row["declared_by"],
                                     action="unit.manual_handling",
                                     resource_type="event_plan", resource_id=plan_row["plan_id"],
                                     detail={"unit_key": unit["unit_key"], "incident_id": incident_row["incident_id"],
                                             "previous_state": state, "reason": kind},
                                     occurred_at=self._now())
                    manual_units.append({"plan_id": plan_row["plan_id"], "unit_key": unit["unit_key"],
                                         "site_id": unit["site_id"], "state": "manual_handling"})
                elif state == "scheduled":
                    # 备选场地就是本次受影响场地时，不存在可迁目标。
                    target = unit["rain_backup_site_id"]
                    if kind == "facility" and target == incident_site:
                        target = None
                    suggestion_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO relocation_suggestions(suggestion_id,incident_id,plan_id,unit_key,"
                        "from_site_id,to_site_id,status,created_at) VALUES(?,?,?,?,?,?, 'pending',?)",
                        (suggestion_id, incident_row["incident_id"], plan_row["plan_id"],
                         unit["unit_key"], unit["site_id"], target, self._now()),
                    )
                    suggestions.append({"suggestion_id": suggestion_id,
                                        "plan_id": plan_row["plan_id"], "unit_key": unit["unit_key"],
                                        "from_site_id": unit["site_id"], "to_site_id": target,
                                        "status": "pending"})
                # ended/cancelled 不受影响。
        return {"manual_units": manual_units, "suggestions": suggestions}

    def declare_rain_alert(self, *, request_id: str, actor_id: str, incident_id: str,
                           severity: str = "warning") -> dict[str, Any]:
        """发布降雨预警：室外进行中单元转人工，未开始单元获得雨天备选迁移建议。"""

        payload = {"actor_id": actor_id, "incident_id": incident_id, "severity": severity, "kind": "rain"}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            incident_id = self._identifier(incident_id, "incident_id")
            severity = self._text(severity, "severity", 40)
            detail = {"kind": "rain", "severity": severity}

            def create():
                now = self._now()
                incident = {"incident_id": incident_id, "kind": "rain", "site_id": None,
                            "severity": severity, "detail": detail, "effective_at": now,
                            "declared_by": actor_id, "created_at": now}
                try:
                    connection.execute(
                        "INSERT INTO incidents(incident_id,kind,site_id,severity,detail_json,effective_at,"
                        "declared_by,created_at) VALUES(?, 'rain', NULL,?,?,?,?,?)",
                        (incident_id, severity, canonical_json(detail), now, actor_id, now),
                    )
                except Exception as exc:
                    raise ConflictError("事件编号已经存在") from exc
                impact = self._apply_incident_impact(connection, incident)
                append_event(connection, actor_id=actor_id, action="incident.rain_declared",
                             resource_type="incident", resource_id=incident_id,
                             detail={"severity": severity, **impact}, occurred_at=now)
                return "incident", incident_id, {"incident_id": incident_id, "kind": "rain",
                                                  "severity": severity, **impact}

            return self._idem(connection, request_id=request_id, action="declare_rain_alert",
                              payload=payload, create=create)

    def report_facility_change(self, *, request_id: str, actor_id: str, incident_id: str,
                               site_id: str, facility_status: str,
                               max_headcount: int | None = None,
                               detail: str = "") -> dict[str, Any]:
        """报告设施条件变化：更新资源版本，并对在场单元执行同样的影响分流。"""

        payload = {"actor_id": actor_id, "incident_id": incident_id, "site_id": site_id,
                   "facility_status": facility_status, "max_headcount": max_headcount, "detail": detail}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._org_site(connection, actor, site_id)
            incident_id = self._identifier(incident_id, "incident_id")
            if facility_status not in {"available", "restricted", "closed"}:
                raise ValidationError("facility_status 不合法")
            resource = connection.execute(
                "SELECT * FROM site_resources WHERE site_id=?", (site_id,)).fetchone()
            if resource is None:
                raise NotFoundError("场地资源尚未配置，请先登记人流上限与室内外标记")
            new_cap = resource["max_headcount"] if max_headcount is None else self._positive_int(
                max_headcount, "max_headcount", allow_zero=True)

            def create():
                now = self._now()
                unchanged = (resource["facility_status"] == facility_status
                             and resource["max_headcount"] == new_cap)
                new_version = resource["version"] if unchanged else resource["version"] + 1
                connection.execute(
                    "UPDATE site_resources SET facility_status=?,max_headcount=?,version=?,updated_at=? WHERE site_id=?",
                    (facility_status, new_cap, new_version, now, site_id),
                )
                incident_detail = {"kind": "facility", "site_id": site_id,
                                   "facility_status": facility_status, "max_headcount": new_cap,
                                   "resource_version": new_version, "detail": detail,
                                   "site_version": site["version"]}
                try:
                    connection.execute(
                        "INSERT INTO incidents(incident_id,kind,site_id,severity,detail_json,effective_at,"
                        "declared_by,created_at) VALUES(?, 'facility',?, 'condition_change',?,?,?,?)",
                        (incident_id, site_id, canonical_json(incident_detail), now, actor_id, now),
                    )
                except Exception as exc:
                    raise ConflictError("事件编号已经存在") from exc
                incident = {"incident_id": incident_id, "kind": "facility", "site_id": site_id,
                            "severity": "condition_change", "detail": incident_detail,
                            "effective_at": now, "declared_by": actor_id, "created_at": now}
                impact = self._apply_incident_impact(connection, incident)
                append_event(connection, actor_id=actor_id, action="incident.facility_reported",
                             resource_type="incident", resource_id=incident_id,
                             detail={**incident_detail, **impact}, occurred_at=now)
                return "incident", incident_id, {"incident_id": incident_id, "kind": "facility",
                                                  "site_id": site_id,
                                                  "facility_status": facility_status,
                                                  "resource_version": new_version, **impact}

            return self._idem(connection, request_id=request_id, action="report_facility_change",
                              payload=payload, create=create)

    def _responsibility_chain(self, connection, gate: dict[str, Any]) -> list[dict[str, Any]]:
        chain = [{"stage": "planning", "actor_id": gate["submitted_by"]}]
        for check in sorted(gate["required_checks"], key=lambda c: (c["scope_type"], c["scope_key"],
                                                                    c["check_kind"])):
            chain.append({"stage": "signoff", "check_id": check["check_id"],
                          "check_kind": check["check_kind"],
                          "scope": f"{check['scope_type']}:{check['scope_key']}",
                          "owner_actor_id": check["owner_actor_id"], "status": check["status"],
                          "signed_by": check["signed_by"], "signed_at": check["signed_at"],
                          "carried": check["carried"]})
        chain.append({"stage": "publish", "actor_id": gate["published_by"],
                      "published_at": gate["published_at"]})
        return chain

    def _relocation_preview(self, connection, plan_row, unit_row, target_site_id: str) -> dict[str, Any]:
        """预览单元迁到备选场地后的闸门条件。"""

        blockers: list[str] = []
        target_resource = connection.execute(
            "SELECT * FROM site_resources WHERE site_id=?", (target_site_id,)).fetchone()
        if target_resource is None:
            blockers.append("备选场地尚未配置人流上限")
        elif target_resource["facility_status"] == "closed":
            blockers.append("备选场地已关闭")
        if target_resource is not None and unit_row["expected_headcount"] > target_resource["max_headcount"]:
            blockers.append(
                f"备选场地上限 {target_resource['max_headcount']} 小于单元人数 {unit_row['expected_headcount']}")
        windows = connection.execute(
            "SELECT * FROM site_windows WHERE site_id=? AND active=1", (target_site_id,)).fetchall()
        if not any(w["window_start"] <= unit_row["start_time"]
                   and unit_row["end_time"] <= w["window_end"] for w in windows):
            blockers.append("单元时间不在备选场地开放窗口内")
        # 容量投影：其他方案承诺 + 本方案其他单元 + 迁移后的本单元。
        entries: list[dict[str, Any]] = []
        for row in connection.execute(
            "SELECT c.*, s.state FROM capacity_commits c JOIN unit_states s"
            " ON c.plan_id=s.plan_id AND c.unit_key=s.unit_key"
            " WHERE c.site_id=? AND s.state NOT IN ('ended','cancelled')",
            (target_site_id,),
        ):
            if row["plan_id"] == plan_row["plan_id"] and row["unit_key"] == unit_row["unit_key"]:
                continue
            entries.append({"start_time": row["start_time"], "end_time": row["end_time"],
                            "headcount": row["headcount"]})
        entries.append({"start_time": unit_row["start_time"], "end_time": unit_row["end_time"],
                        "headcount": unit_row["expected_headcount"]})
        if target_resource is not None:
            peak = max((sum(e["headcount"] for e in entries
                            if e["start_time"] < anchor["end_time"] and e["end_time"] > anchor["start_time"])
                        for anchor in entries), default=0)
            if peak > target_resource["max_headcount"]:
                blockers.append(f"迁移后备选场地重叠峰值 {peak} 超过上限 {target_resource['max_headcount']}")
        checks = self._checks_for(connection, plan_row["organization_id"])
        missing = [c for c in checks if c["scope_type"] == "site" and c["scope_key"] == target_site_id]
        if not any(c["check_kind"] == "fire" for c in missing):
            blockers.append(f"备选场地 {target_site_id} 缺少消防检查项与负责安全员")
        return {"target_site_id": target_site_id, "blockers": blockers, "feasible": not blockers}

    def analyze_impact(self, plan_id: str | None = None) -> dict[str, Any]:
        """展示预警下的分流结果、迁移可行性以及调整前后的责任链。"""

        connection = self.database.connection
        suggestions = connection.execute(
            "SELECT * FROM relocation_suggestions WHERE status!='discarded' ORDER BY created_at",
        ).fetchall()
        if plan_id:
            suggestions = [s for s in suggestions if s["plan_id"] == plan_id]
        items: list[dict[str, Any]] = []
        for suggestion in suggestions:
            plan_row = connection.execute(
                "SELECT * FROM event_plans WHERE plan_id=?", (suggestion["plan_id"],)).fetchone()
            revision = self._load_revision(connection, suggestion["plan_id"], status="published")
            if plan_row is None or revision is None:
                continue
            unit = connection.execute(
                "SELECT * FROM plan_units WHERE plan_id=? AND revision_no=? AND unit_key=?",
                (suggestion["plan_id"], revision["revision_no"], suggestion["unit_key"]),
            ).fetchone()
            state_row = self._state_map(connection, suggestion["plan_id"]).get(suggestion["unit_key"])
            gate_before = self._evaluate(
                connection, plan_row, revision,
                self._unit_rows(connection, suggestion["plan_id"], revision["revision_no"]))
            item: dict[str, Any] = {
                "suggestion_id": suggestion["suggestion_id"],
                "incident_id": suggestion["incident_id"],
                "plan_id": suggestion["plan_id"], "unit_key": suggestion["unit_key"],
                "unit_version": unit["version"], "state": state_row["state"] if state_row else "scheduled",
                "from_site_id": suggestion["from_site_id"],
                "to_site_id": suggestion["to_site_id"], "status": suggestion["status"],
                "responsibility_chain_before": self._responsibility_chain(connection, gate_before),
            }
            if state_row is not None and state_row["state"] != "scheduled":
                if state_row["state"] in TERMINAL_STATES:
                    item["note"] = f"单元已{('结束' if state_row['state']=='ended' else '取消')}，迁移建议不再适用"
                else:
                    item["note"] = "单元已经开始，迁移建议自动失效，须走人工处置"
                item["preview"] = None
            elif not suggestion["to_site_id"]:
                item["preview"] = {"target_site_id": None, "feasible": False,
                                   "blockers": ["没有可用的迁移目标：未登记雨天备选场地，或备选场地同样受到本次事件影响"]}
            else:
                item["preview"] = self._relocation_preview(
                    connection, plan_row, unit, suggestion["to_site_id"])
                # 调整后责任链：以已接受的草稿修订为准，否则按迁移投影出预期链。
                draft = self._load_revision(connection, suggestion["plan_id"], status="drafting")
                if draft is not None:
                    draft_gate = self._evaluate(
                        connection, plan_row, draft,
                        self._unit_rows(connection, suggestion["plan_id"], draft["revision_no"]))
                    item["responsibility_chain_after"] = self._responsibility_chain(connection, draft_gate)
                else:
                    after = [{"stage": "planning", "actor_id": revision["submitted_by"]}]
                    moving = suggestion["unit_key"]
                    remaining_sites = {u["site_id"] for u in
                                       self._unit_rows(connection, suggestion["plan_id"],
                                                       revision["revision_no"])
                                       if u["unit_key"] != moving}
                    projected: dict[tuple[str, str], dict[str, Any]] = {}
                    for check in gate_before["required_checks"]:
                        if check["check_id"] is None:
                            continue
                        if check["scope_type"] == "site":
                            if check["scope_key"] == suggestion["to_site_id"]:
                                continue
                            new_status = ("carried" if check["scope_key"] in remaining_sites
                                          else "stale")
                        elif check["scope_key"] == moving:
                            new_status = "stale"  # 单元资源变化，旧签失效
                        else:
                            new_status = "carried"
                        projected[(check["scope_type"], check["scope_key"])] = {
                            "stage": "signoff", "check_id": check["check_id"],
                            "check_kind": check["check_kind"],
                            "scope": f"{check['scope_type']}:{check['scope_key']}",
                            "owner_actor_id": check["owner_actor_id"],
                            "status": new_status,
                            "signed_by": check["signed_by"] if new_status == "carried" else None,
                            "signed_at": check["signed_at"] if new_status == "carried" else None,
                            "carried": new_status == "carried"}
                    target_fire = next((c for c in self._checks_for(connection, plan_row["organization_id"])
                                        if c["scope_type"] == "site"
                                        and c["scope_key"] == suggestion["to_site_id"]
                                        and c["check_kind"] == "fire"), None)
                    projected[("site", suggestion["to_site_id"] or "")] = {
                        "stage": "signoff",
                        "check_id": target_fire["check_id"] if target_fire else None,
                        "check_kind": "fire",
                        "scope": f"site:{suggestion['to_site_id']}",
                        "owner_actor_id": target_fire["owner_actor_id"] if target_fire else None,
                        "status": "missing", "signed_by": None, "signed_at": None,
                        "carried": False}
                    after.extend(projected[key] for key in sorted(projected))
                    after.append({"stage": "publish", "actor_id": None, "published_at": None})
                    item["responsibility_chain_after"] = after
            items.append(item)

        manual = []
        plans = connection.execute("SELECT * FROM event_plans ORDER BY plan_id").fetchall()
        for plan_row in plans:
            if plan_id and plan_row["plan_id"] != plan_id:
                continue
            for row in connection.execute(
                "SELECT * FROM unit_states WHERE plan_id=? AND state='manual_handling' ORDER BY unit_key",
                (plan_row["plan_id"],),
            ):
                manual.append({"plan_id": plan_row["plan_id"], "unit_key": row["unit_key"],
                               "state": "manual_handling", "revision_no": row["revision_no"]})
        return {"relocations": items, "manual_units": manual}

    def accept_relocation(self, *, request_id: str, actor_id: str, plan_id: str, unit_key: str,
                          target_site_id: str | None = None) -> dict[str, Any]:
        """接受迁移建议：生成新草稿修订，新场地检查项必须重新签署后才能发布。"""

        payload = {"actor_id": actor_id, "plan_id": plan_id, "unit_key": unit_key,
                   "target_site_id": target_site_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            plan_row = connection.execute("SELECT * FROM event_plans WHERE plan_id=?", (plan_id,)).fetchone()
            if plan_row is None:
                raise NotFoundError("方案不存在")

            def create():
                published = self._load_revision(connection, plan_id, status="published")
                if published is None:
                    raise ConflictError("方案尚未发布，不能接受迁移建议；请直接修改草稿")
                state_row = self._state_map(connection, plan_id).get(unit_key)
                if state_row is None:
                    raise NotFoundError("单元不存在")
                if state_row["state"] != "scheduled":
                    raise ConflictError("只有未开始单元可以迁移；进行中单元必须人工处置，不能静默换场")
                base_revision = self._load_revision(connection, plan_id, status="drafting") or published
                units = self._unit_rows(connection, plan_id, base_revision["revision_no"])
                moving = next((u for u in units if u["unit_key"] == unit_key), None)
                if moving is None:
                    raise NotFoundError("单元不在当前修订中")
                suggestion = connection.execute(
                    "SELECT * FROM relocation_suggestions WHERE plan_id=? AND unit_key=? AND status='pending'"
                    " ORDER BY created_at DESC LIMIT 1", (plan_id, unit_key),
                ).fetchone()
                if suggestion is None:
                    raise ConflictError("没有待处理的迁移建议")
                target = target_site_id or suggestion["to_site_id"]
                if not target:
                    raise ValidationError("单元没有雨天备选场地，无法给出迁移目标")
                target = self._identifier(target, "target_site_id")
                self._org_site(connection, actor, target)
                target_resource = connection.execute(
                    "SELECT * FROM site_resources WHERE site_id=?", (target,)).fetchone()
                if target_resource is None or target_resource["facility_status"] == "closed":
                    raise ConflictError("目标场地不可用，迁移不能成立")

                now = self._now()
                if base_revision["status"] == "drafting":
                    revision_no = base_revision["revision_no"]
                    connection.execute(
                        "DELETE FROM plan_units WHERE plan_id=? AND revision_no=?",
                        (plan_id, revision_no),
                    )
                else:
                    revision_no = base_revision["revision_no"] + 1
                    connection.execute(
                        "INSERT INTO plan_revisions(plan_id,revision_no,status,submitted_by,payload_hash,created_at)"
                        " VALUES(?,?,'drafting',?,?,?)",
                        (plan_id, revision_no, actor_id, "", now),
                    )
                unit_payloads = []
                changed_views = []
                for row in units:
                    posts = json.loads(row["posts_json"])
                    depends = json.loads(row["depends_on_json"])
                    new_site = target if row["unit_key"] == unit_key else row["site_id"]
                    unit_hash_input = {"name": row["name"], "site_id": new_site,
                                       "start_time": row["start_time"], "end_time": row["end_time"],
                                       "expected_headcount": row["expected_headcount"], "posts": posts,
                                       "depends_on": depends,
                                       "rain_backup_site_id": row["rain_backup_site_id"]}
                    new_hash = digest({k: v for k, v in unit_hash_input.items()})
                    version = row["version"] + 1 if (
                        row["unit_key"] == unit_key and row["payload_hash"] != new_hash) else row["version"]
                    connection.execute(
                        "INSERT INTO plan_units(plan_id,revision_no,unit_key,version,name,site_id,start_time,"
                        "end_time,expected_headcount,posts_json,depends_on_json,rain_backup_site_id,payload_hash)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (plan_id, revision_no, row["unit_key"], version, row["name"], new_site,
                         row["start_time"], row["end_time"], row["expected_headcount"],
                         canonical_json(posts), canonical_json(depends),
                         row["rain_backup_site_id"], new_hash),
                    )
                    unit_payloads.append({"key": row["unit_key"], **unit_hash_input})
                    if row["unit_key"] == unit_key:
                        changed_views.append({"unit_key": unit_key, "from_site_id": row["site_id"],
                                              "to_site_id": target, "version": version})
                new_payload_hash = digest(sorted(unit_payloads, key=lambda u: u["key"]))
                connection.execute(
                    "UPDATE plan_revisions SET payload_hash=? WHERE plan_id=? AND revision_no=?",
                    (new_payload_hash, plan_id, revision_no),
                )
                connection.execute(
                    "UPDATE relocation_suggestions SET status='accepted',to_site_id=?"
                    " WHERE suggestion_id=?", (target, suggestion["suggestion_id"]),
                )
                append_event(connection, actor_id=actor_id, action="relocation.accepted",
                             resource_type="event_plan", resource_id=plan_id,
                             detail={"revision_no": revision_no, "changes": changed_views,
                                     "suggestion_id": suggestion["suggestion_id"]},
                             occurred_at=now)
                return "event_plan", plan_id, {"plan_id": plan_id, "revision_no": revision_no,
                                                "status": "drafting", "changes": changed_views}

            return self._idem(connection, request_id=request_id, action="accept_relocation",
                              payload=payload, create=create)

    # ------------------------------------------------------------- 单元生命周期

    def _transition(self, *, request_id: str, actor_id: str, plan_id: str, unit_key: str,
                    action: str, extra_payload: dict[str, Any] | None = None,
                    allowed_roles=("admin", "operator")) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "plan_id": plan_id, "unit_key": unit_key,
                   "action": action, **(extra_payload or {})}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *allowed_roles)
            plan_row = connection.execute("SELECT * FROM event_plans WHERE plan_id=?", (plan_id,)).fetchone()
            if plan_row is None:
                raise NotFoundError("方案不存在")

            def create():
                revision = self._load_revision(connection, plan_id, status="published")
                if revision is None:
                    raise ConflictError("方案尚未发布，单元不能执行现场动作")
                unit = connection.execute(
                    "SELECT * FROM plan_units WHERE plan_id=? AND revision_no=? AND unit_key=?",
                    (plan_id, revision["revision_no"], unit_key),
                ).fetchone()
                if unit is None:
                    raise NotFoundError("单元不存在")
                state_row = self._state_map(connection, plan_id).get(unit_key)
                current = state_row["state"] if state_row else "scheduled"
                allowed, target = TRANSITIONS[action]
                if current == target:
                    return "unit", f"{plan_id}:{unit_key}", {"plan_id": plan_id, "unit_key": unit_key,
                                                              "state": current, "reapplied": True}
                hint = ORDER_HINTS.get((action, current))
                if current not in allowed:
                    raise ConflictError(hint or f"当前状态 {current} 不允许执行 {action}")
                now = self._now()
                connection.execute(
                    "UPDATE unit_states SET state=?,revision_no=?,updated_by=?,updated_at=?"
                    " WHERE plan_id=? AND unit_key=?",
                    (target, revision["revision_no"], actor_id, now, plan_id, unit_key),
                )
                # 取消后容量释放由账本查询按状态过滤完成，保留承诺记录用于审计。
                append_event(connection, actor_id=actor_id, action=TRANSITION_ACTIONS[action],
                             resource_type="event_plan", resource_id=plan_id,
                             detail={"unit_key": unit_key, "from_state": current, "to_state": target,
                                     "revision_no": revision["revision_no"]},
                             occurred_at=now)
                return "unit", f"{plan_id}:{unit_key}", {"plan_id": plan_id, "unit_key": unit_key,
                                                          "state": target, "reapplied": False}

            return self._idem(connection, request_id=request_id, action=f"unit_{action}",
                              payload=payload, create=create)

    def start_unit(self, **kwargs) -> dict[str, Any]:
        return self._transition(action="start", **kwargs)

    def pause_unit(self, **kwargs) -> dict[str, Any]:
        return self._transition(action="pause", **kwargs)

    def resume_unit(self, **kwargs) -> dict[str, Any]:
        return self._transition(action="resume", **kwargs)

    def end_unit(self, **kwargs) -> dict[str, Any]:
        return self._transition(action="end", **kwargs)

    def cancel_unit(self, **kwargs) -> dict[str, Any]:
        return self._transition(action="cancel", **kwargs)

    def resolve_manual_unit(self, *, request_id: str, actor_id: str, plan_id: str,
                            unit_key: str, decision: str) -> dict[str, Any]:
        """人工处置进行中单元：resume 继续活动，end 结束活动。"""

        if decision not in ("resume", "end"):
            raise ValidationError("decision 只能是 resume 或 end")
        payload = {"actor_id": actor_id, "plan_id": plan_id, "unit_key": unit_key,
                   "action": "resolve_manual", "decision": decision}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            plan_row = connection.execute("SELECT * FROM event_plans WHERE plan_id=?", (plan_id,)).fetchone()
            if plan_row is None:
                raise NotFoundError("方案不存在")

            def create():
                revision = self._load_revision(connection, plan_id, status="published")
                state_row = self._state_map(connection, plan_id).get(unit_key)
                if state_row is None:
                    raise NotFoundError("单元不存在")
                current = state_row["state"]
                target = "in_progress" if decision == "resume" else "ended"
                if current == target and decision == "resume":
                    return "unit", f"{plan_id}:{unit_key}", {"plan_id": plan_id, "unit_key": unit_key,
                                                              "state": target, "reapplied": True}
                if current == "ended" and decision == "end":
                    return "unit", f"{plan_id}:{unit_key}", {"plan_id": plan_id, "unit_key": unit_key,
                                                              "state": "ended", "reapplied": True}
                if current != "manual_handling":
                    raise ConflictError(f"单元当前状态为 {current}，人工处置只能作用于 manual_handling 状态")
                now = self._now()
                connection.execute(
                    "UPDATE unit_states SET state=?,updated_by=?,updated_at=? WHERE plan_id=? AND unit_key=?",
                    (target, actor_id, now, plan_id, unit_key),
                )
                append_event(connection, actor_id=actor_id, action="unit.manual_resolved",
                             resource_type="event_plan", resource_id=plan_id,
                             detail={"unit_key": unit_key, "decision": decision,
                                     "from_state": "manual_handling", "to_state": target},
                             occurred_at=now)
                return "unit", f"{plan_id}:{unit_key}", {"plan_id": plan_id, "unit_key": unit_key,
                                                          "state": target, "reapplied": False}

            return self._idem(connection, request_id=request_id, action="resolve_manual_unit",
                              payload=payload, create=create)
