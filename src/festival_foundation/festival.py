"""提供中秋活动编排、容量账本、安全签署与影响分析能力。

本模块叠加在基础层（组织、操作者、站点、幂等、审计）之上，实现：

- 策划人提交带版本的活动单元与依赖关系（无环有向图）；
- 不同人员分别维护站点开放窗口、岗位资格、物资检查项、人流上限与雨天备选方案，
  每项资源独立带版本并记录维护人；
- 系统结合上述资源生成可执行方案，方案锁定单元版本与资源版本快照；
- 安全员只能签署自己负责的检查项，必需签署齐备且资源版本未变化时方案才可一次性发布；
- 取消、暂停、恢复、结束遵守明确状态顺序，重复操作返回稳定结果；
- 降雨预警或设施变化只对未开始单元给出迁移建议，进行中的单元进入人工处置。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from .audit import append_event, canonical_json
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .festival_models import (ActivityUnit, CapacityEntry, ImpactAssessment, ResourceState,
                              SignOff)
from .service import IDENTIFIER, DomainService
from .storage import Database


UNIT_KINDS = frozenset({"poetry", "lantern", "folk", "other"})
RESOURCE_TYPES = frozenset({
    "site_window",      # 站点开放窗口
    "qualification",    # 岗位资格（resource_key 为人员编号）
    "material_check",   # 物资/消防检查项（resource_key 为检查项名称）
    "capacity_limit",   # 人流上限
    "rain_fallback",    # 雨天备选方案
})
IMPACT_KINDS = frozenset({"rain_alert", "facility_change", "capacity_change"})
OPEN_STATUSES = ("draft", "scheduled")
ACTIVE_STATUSES = frozenset({"in_progress", "paused"})
TERMINAL_STATUSES = frozenset({"finished", "cancelled"})
NOT_STARTED_STATUSES = ("scheduled", "released")
TRANSITIONS = {
    "start": {"released": "in_progress"},
    "pause": {"in_progress": "paused"},
    "resume": {"paused": "in_progress"},
    "finish": {"in_progress": "finished", "paused": "finished"},
    "cancel": {"draft": "cancelled", "scheduled": "cancelled", "released": "cancelled"},
}
TRANSITION_EVENTS = {
    "start": "festival.unit.started",
    "pause": "festival.unit.paused",
    "resume": "festival.unit.resumed",
    "finish": "festival.unit.finished",
    "cancel": "festival.unit.cancelled",
}


def _parse_time(value: Any, field: str) -> datetime:
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} 必须是 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} 必须包含时区")
    return parsed.astimezone(timezone.utc)


def _format_time(value: datetime) -> str:
    """把时间规范化为定长 UTC 文本，使字符串比较等价于时间比较。"""

    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _overlaps(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    return a_start < b_end and b_start < a_end


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(f"{field} 必须是正整数")
    return value


def _string_list(value: Iterable[Any] | None, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"{field} 必须是字符串数组")
    items: list[str] = []
    for item in value:
        text = str(item).strip()
        if not IDENTIFIER.fullmatch(text):
            raise ValidationError(f"{field} 含有格式无效的项")
        items.append(text)
    return sorted(set(items))


class FestivalService:
    """协调活动编排、容量账本、签署放行与影响分析规则。"""

    def __init__(self, domain: DomainService) -> None:
        self.domain = domain
        self.database: Database = domain.database

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return self.domain._now()

    def _site_row(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row

    def _check_site_scope(self, actor, site_row) -> None:
        if actor.role != "admin" and actor.organization_id != site_row["organization_id"]:
            raise PermissionDenied("不能操作其他组织的场所")

    def _unit_row(self, connection, unit_id: str):
        row = connection.execute("SELECT * FROM festival_units WHERE unit_id=?", (unit_id,)).fetchone()
        if row is None:
            raise NotFoundError("活动单元不存在")
        return row

    def _plan_row(self, connection, plan_id: str):
        row = connection.execute("SELECT * FROM festival_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError("方案不存在")
        return row

    def _resource_row(self, connection, site_id: str, resource_type: str, resource_key: str):
        return connection.execute(
            "SELECT * FROM festival_resources WHERE site_id=? AND resource_type=? AND resource_key=?",
            (site_id, resource_type, resource_key),
        ).fetchone()

    def _unit_dependencies(self, connection, unit_id: str) -> list[str]:
        rows = connection.execute(
            "SELECT depends_on FROM festival_dependencies WHERE unit_id=? ORDER BY depends_on",
            (unit_id,),
        ).fetchall()
        return [row["depends_on"] for row in rows]

    def _assert_acyclic(self, connection, site_id: str) -> None:
        """对站点内全部依赖边做三色标记检测，发现环即拒绝。"""

        rows = connection.execute(
            "SELECT d.unit_id, d.depends_on FROM festival_dependencies d "
            "JOIN festival_units u ON u.unit_id=d.unit_id WHERE u.site_id=?",
            (site_id,),
        ).fetchall()
        graph: dict[str, list[str]] = {}
        for row in rows:
            graph.setdefault(row["unit_id"], []).append(row["depends_on"])
        color: dict[str, int] = {}

        def visit(node: str) -> None:
            color[node] = 1
            for nxt in graph.get(node, []):
                state = color.get(nxt, 0)
                if state == 1:
                    raise ValidationError("活动单元依赖关系存在环")
                if state == 0:
                    visit(nxt)
            color[node] = 2

        for node in graph:
            if color.get(node, 0) == 0:
                visit(node)

    # ------------------------------------------------------------------
    # 活动单元与依赖
    # ------------------------------------------------------------------

    def submit_unit(self, *, request_id: str, actor_id: str, unit_id: str, site_id: str,
                    title: str, kind: str, planned_start: str, planned_end: str,
                    expected_headcount: int, required_qualifications: Iterable[str] = (),
                    required_checks: Iterable[str] = (),
                    dependencies: Iterable[str] = ()) -> dict[str, Any]:
        """提交或修订一个带版本的活动单元；内容不变时保持原版本。"""

        full_payload = {"actor_id": actor_id, "unit_id": unit_id, "site_id": site_id, "title": title,
                        "kind": kind, "planned_start": planned_start, "planned_end": planned_end,
                        "expected_headcount": expected_headcount,
                        "required_qualifications": list(required_qualifications or ()),
                        "required_checks": list(required_checks or ()),
                        "dependencies": list(dependencies or ())}
        with self.database.transaction(immediate=True) as connection:
            actor = self.domain._actor(connection, actor_id)
            self.domain._require(actor, "admin", "operator")
            site = self._site_row(connection, site_id)
            self._check_site_scope(actor, site)
            unit_id = self.domain._identifier(unit_id, "unit_id")
            title = self.domain._text(title, "title")
            if kind not in UNIT_KINDS:
                raise ValidationError("活动类型不在允许范围内")
            start = _parse_time(planned_start, "planned_start")
            end = _parse_time(planned_end, "planned_end")
            if not start < end:
                raise ValidationError("计划开始必须早于计划结束")
            headcount = _positive_int(expected_headcount, "expected_headcount")
            qualifications = _string_list(required_qualifications, "required_qualifications")
            checks = _string_list(required_checks, "required_checks")
            deps = _string_list(dependencies, "dependencies")
            if unit_id in deps:
                raise ValidationError("活动单元不能依赖自身")
            for dep in deps:
                dep_row = connection.execute(
                    "SELECT site_id FROM festival_units WHERE unit_id=?", (dep,)).fetchone()
                if dep_row is None:
                    raise NotFoundError(f"依赖的活动单元 {dep} 不存在")
                if dep_row["site_id"] != site_id:
                    raise ValidationError("依赖的活动单元必须属于同一站点")
            start_text, end_text = _format_time(start), _format_time(end)

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM festival_units WHERE unit_id=?", (unit_id,)).fetchone()
                now = self._now()
                if existing is not None:
                    unchanged = (
                        existing["title"] == title and existing["kind"] == kind
                        and existing["planned_start"] == start_text and existing["planned_end"] == end_text
                        and existing["expected_headcount"] == headcount
                        and json.loads(existing["required_qualifications_json"]) == qualifications
                        and json.loads(existing["required_checks_json"]) == checks
                        and self._unit_dependencies(connection, unit_id) == deps
                    )
                    if unchanged:
                        return "festival_unit", unit_id, {
                            "unit_id": unit_id, "site_id": site_id, "version": existing["version"],
                            "status": existing["status"], "noop": True}
                    if existing["status"] not in OPEN_STATUSES:
                        raise ConflictError("已发布或进行中的活动单元不能直接修改，请先取消")
                    version = existing["version"] + 1
                    connection.execute(
                        "UPDATE festival_units SET title=?, kind=?, planned_start=?, planned_end=?, "
                        "expected_headcount=?, required_qualifications_json=?, required_checks_json=?, "
                        "version=?, updated_at=? WHERE unit_id=?",
                        (title, kind, start_text, end_text, headcount, canonical_json(qualifications),
                         canonical_json(checks), version, now, unit_id))
                    connection.execute("DELETE FROM festival_dependencies WHERE unit_id=?", (unit_id,))
                    action = "festival.unit.revised"
                    status = existing["status"]
                else:
                    version = 1
                    status = "draft"
                    connection.execute(
                        "INSERT INTO festival_units(unit_id,site_id,title,kind,planned_start,planned_end,"
                        "expected_headcount,required_qualifications_json,required_checks_json,version,status,"
                        "created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (unit_id, site_id, title, kind, start_text, end_text, headcount,
                         canonical_json(qualifications), canonical_json(checks), version, status,
                         actor_id, now, now))
                    action = "festival.unit.submitted"
                for dep in deps:
                    connection.execute(
                        "INSERT INTO festival_dependencies(unit_id,depends_on) VALUES(?,?)", (unit_id, dep))
                self._assert_acyclic(connection, site_id)
                append_event(connection, actor_id=actor_id, action=action,
                             resource_type="festival_unit", resource_id=unit_id,
                             detail={"site_id": site_id, "version": version, "kind": kind,
                                     "dependencies": deps}, occurred_at=now)
                return "festival_unit", unit_id, {
                    "unit_id": unit_id, "site_id": site_id, "version": version,
                    "status": status, "noop": False}

            return self.domain.run_idempotent(connection, request_id=request_id,
                                              action="submit_festival_unit",
                                              payload=full_payload, create=create)

    # ------------------------------------------------------------------
    # 保障资源维护（站点窗口 / 岗位资格 / 物资检查 / 人流上限 / 雨天备选）
    # ------------------------------------------------------------------

    def _validate_resource_payload(self, connection, site_row, resource_type: str,
                                   resource_key: str, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict) or not payload:
            raise ValidationError("payload 必须是非空对象")
        site_id = site_row["site_id"]
        organization_id = site_row["organization_id"]
        if resource_type == "site_window":
            if resource_key != site_id:
                raise ValidationError("站点开放窗口的 resource_key 必须等于 site_id")
            windows = payload.get("windows")
            if not isinstance(windows, list) or not windows:
                raise ValidationError("windows 必须是非空数组")
            normalized = []
            for window in windows:
                if not isinstance(window, dict):
                    raise ValidationError("windows 的每一项必须是对象")
                start = _parse_time(window.get("start"), "windows.start")
                end = _parse_time(window.get("end"), "windows.end")
                if not start < end:
                    raise ValidationError("开放窗口开始必须早于结束")
                normalized.append({"start": _format_time(start), "end": _format_time(end)})
            normalized.sort(key=lambda item: item["start"])
            return {"windows": normalized}
        if resource_type == "capacity_limit":
            if resource_key != site_id:
                raise ValidationError("人流上限的 resource_key 必须等于 site_id")
            return {"max_headcount": _positive_int(payload.get("max_headcount"), "max_headcount")}
        if resource_type == "qualification":
            qualifications = _string_list(payload.get("qualifications"), "qualifications")
            if not qualifications:
                raise ValidationError("qualifications 不能为空")
            holder = connection.execute(
                "SELECT * FROM actors WHERE actor_id=?", (resource_key,)).fetchone()
            if holder is None:
                raise NotFoundError("资质人员不存在")
            if not holder["active"]:
                raise ValidationError("资质人员已停用")
            if holder["organization_id"] != organization_id:
                raise ValidationError("资质人员不属于站点组织")
            return {"qualifications": qualifications}
        if resource_type == "material_check":
            responsible = str(payload.get("responsible_actor", "")).strip()
            owner = connection.execute(
                "SELECT * FROM actors WHERE actor_id=?", (responsible,)).fetchone()
            if owner is None:
                raise NotFoundError("检查项负责安全员不存在")
            if not owner["active"] or owner["role"] != "reviewer":
                raise ValidationError("检查项负责人必须是未停用的安全员(reviewer)")
            if owner["organization_id"] != organization_id:
                raise ValidationError("检查项负责人不属于站点组织")
            description = str(payload.get("description", "")).strip()[:200]
            return {"responsible_actor": responsible, "description": description}
        if resource_type == "rain_fallback":
            fallback_site = str(payload.get("fallback_site_id", "")).strip()
            fallback = connection.execute(
                "SELECT * FROM sites WHERE site_id=?", (fallback_site,)).fetchone()
            if fallback is None:
                raise NotFoundError("雨天备选场地不存在")
            if fallback["organization_id"] != organization_id:
                raise ValidationError("雨天备选场地必须属于同一组织")
            note = str(payload.get("note", "")).strip()[:200]
            return {"fallback_site_id": fallback_site, "note": note}
        raise ValidationError("资源类型不在允许范围内")

    def update_resource(self, *, request_id: str, actor_id: str, site_id: str,
                        resource_type: str, resource_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        """维护一项保障资源；内容变化时版本递增，内容不变时保持稳定。"""

        if resource_type not in RESOURCE_TYPES:
            raise ValidationError("资源类型不在允许范围内")
        full_payload = {"actor_id": actor_id, "site_id": site_id, "resource_type": resource_type,
                        "resource_key": resource_key, "payload": payload}
        with self.database.transaction(immediate=True) as connection:
            actor = self.domain._actor(connection, actor_id)
            self.domain._require(actor, "admin", "operator")
            site = self._site_row(connection, site_id)
            self._check_site_scope(actor, site)
            resource_key = self.domain._identifier(resource_key, "resource_key")
            normalized = self._validate_resource_payload(connection, site, resource_type,
                                                         resource_key, payload)
            resource_id = f"{site_id}/{resource_type}/{resource_key}"

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = self._resource_row(connection, site_id, resource_type, resource_key)
                if existing is not None and existing["payload_json"] == canonical_json(normalized):
                    return "festival_resource", resource_id, {
                        "site_id": site_id, "resource_type": resource_type, "resource_key": resource_key,
                        "version": existing["version"], "maintained_by": existing["maintained_by"],
                        "noop": True}
                version = existing["version"] + 1 if existing is not None else 1
                now = self._now()
                if existing is not None:
                    connection.execute(
                        "UPDATE festival_resources SET payload_json=?, version=?, maintained_by=?, updated_at=? "
                        "WHERE site_id=? AND resource_type=? AND resource_key=?",
                        (canonical_json(normalized), version, actor_id, now,
                         site_id, resource_type, resource_key))
                else:
                    connection.execute(
                        "INSERT INTO festival_resources(site_id,resource_type,resource_key,payload_json,"
                        "version,maintained_by,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (site_id, resource_type, resource_key, canonical_json(normalized),
                         version, actor_id, now))
                append_event(connection, actor_id=actor_id, action="festival.resource.updated",
                             resource_type="festival_resource", resource_id=resource_id,
                             detail={"resource_type": resource_type, "resource_key": resource_key,
                                     "version": version, "maintained_by": actor_id}, occurred_at=now)
                return "festival_resource", resource_id, {
                    "site_id": site_id, "resource_type": resource_type, "resource_key": resource_key,
                    "version": version, "maintained_by": actor_id, "noop": False}

            return self.domain.run_idempotent(connection, request_id=request_id,
                                              action="update_festival_resource",
                                              payload=full_payload, create=create)

    # ------------------------------------------------------------------
    # 放行评估（方案生成、发布与 clearance 接口共用）
    # ------------------------------------------------------------------

    def _find_qualified(self, connection, site_id: str, qualification: str):
        rows = connection.execute(
            "SELECT r.*, a.active AS holder_active FROM festival_resources r "
            "JOIN actors a ON a.actor_id=r.resource_key "
            "WHERE r.site_id=? AND r.resource_type='qualification' ORDER BY r.resource_key",
            (site_id,),
        ).fetchall()
        for row in rows:
            if not row["holder_active"]:
                continue
            if qualification in json.loads(row["payload_json"])["qualifications"]:
                return row
        return None

    def _overlap_headcount(self, connection, site_id: str, unit, batch,
                           exclude_plan_id: str | None) -> int:
        """计算与单元时间重叠的批次内人数与账本占用之和。"""

        total = 0
        for other in batch:
            if _overlaps(unit["planned_start"], unit["planned_end"],
                         other["planned_start"], other["planned_end"]):
                total += other["expected_headcount"]
        rows = connection.execute(
            "SELECT slot_start, slot_end, headcount FROM festival_capacity_ledger "
            "WHERE site_id=? AND state IN ('reserved','committed') AND plan_id != ?",
            (site_id, exclude_plan_id or ""),
        ).fetchall()
        for row in rows:
            if _overlaps(unit["planned_start"], unit["planned_end"],
                         row["slot_start"], row["slot_end"]):
                total += row["headcount"]
        return total

    def _evaluate_unit(self, connection, site_id: str, unit, batch,
                       exclude_plan_id: str | None) -> tuple[list[dict[str, Any]], dict[str, str],
                                                             dict[str, dict[str, Any]], dict[str, str]]:
        """返回 (阻挡条件, 岗位人员安排, 资源版本引用, 检查项负责人)。"""

        blockers: list[dict[str, Any]] = []
        staffing: dict[str, str] = {}
        refs: dict[str, dict[str, Any]] = {}
        check_owners: dict[str, str] = {}

        window = self._resource_row(connection, site_id, "site_window", site_id)
        if window is None:
            blockers.append({"code": "missing_site_window", "message": "站点未登记开放窗口"})
        else:
            refs[f"site_window:{site_id}"] = {"version": window["version"],
                                              "maintained_by": window["maintained_by"]}
            windows = json.loads(window["payload_json"])["windows"]
            if not any(item["start"] <= unit["planned_start"] and unit["planned_end"] <= item["end"]
                       for item in windows):
                blockers.append({"code": "outside_open_window",
                                 "message": "计划时间不在站点开放窗口内"})

        capacity = self._resource_row(connection, site_id, "capacity_limit", site_id)
        if capacity is None:
            blockers.append({"code": "missing_capacity_limit", "message": "站点未登记人流上限"})
        else:
            refs[f"capacity_limit:{site_id}"] = {"version": capacity["version"],
                                                 "maintained_by": capacity["maintained_by"]}
            limit = json.loads(capacity["payload_json"])["max_headcount"]
            needed = self._overlap_headcount(connection, site_id, unit, batch, exclude_plan_id)
            if needed > limit:
                blockers.append({"code": "capacity_exceeded",
                                 "message": "同时段人流超过站点上限",
                                 "limit": limit, "required": needed})

        for qualification in json.loads(unit["required_qualifications_json"]):
            holder = self._find_qualified(connection, site_id, qualification)
            if holder is None:
                blockers.append({"code": "missing_qualification",
                                 "message": f"岗位资格 {qualification} 无合格人员",
                                 "qualification": qualification})
            else:
                staffing[qualification] = holder["resource_key"]
                refs[f"qualification:{holder['resource_key']}"] = {
                    "version": holder["version"], "maintained_by": holder["maintained_by"]}

        for check in json.loads(unit["required_checks_json"]):
            row = self._resource_row(connection, site_id, "material_check", check)
            if row is None:
                blockers.append({"code": "missing_check_item",
                                 "message": f"检查项 {check} 未登记", "check_item": check})
                continue
            refs[f"material_check:{check}"] = {"version": row["version"],
                                               "maintained_by": row["maintained_by"]}
            owner_id = json.loads(row["payload_json"])["responsible_actor"]
            owner = connection.execute(
                "SELECT active, role FROM actors WHERE actor_id=?", (owner_id,)).fetchone()
            if owner is None or not owner["active"] or owner["role"] != "reviewer":
                blockers.append({"code": "missing_check_owner",
                                 "message": f"检查项 {check} 缺少有效安全员", "check_item": check})
            else:
                check_owners[check] = owner_id

        for dep in self._unit_dependencies(connection, unit["unit_id"]):
            dep_row = connection.execute(
                "SELECT status, planned_end FROM festival_units WHERE unit_id=?", (dep,)).fetchone()
            if dep_row is None:
                blockers.append({"code": "dependency_missing",
                                 "message": f"依赖的活动单元 {dep} 不存在", "depends_on": dep})
            elif dep_row["status"] == "cancelled":
                blockers.append({"code": "dependency_cancelled",
                                 "message": f"依赖的活动单元 {dep} 已取消", "depends_on": dep})
            elif dep_row["planned_end"] > unit["planned_start"]:
                blockers.append({"code": "dependency_order",
                                 "message": f"依赖的活动单元 {dep} 计划结束晚于本单元开始",
                                 "depends_on": dep})
        return blockers, staffing, refs, check_owners

    # ------------------------------------------------------------------
    # 方案生成与发布
    # ------------------------------------------------------------------

    def _select_units(self, connection, site_id: str, unit_ids: Iterable[str] | None):
        if unit_ids is None:
            return connection.execute(
                "SELECT * FROM festival_units WHERE site_id=? AND status IN ('draft','scheduled') "
                "ORDER BY unit_id", (site_id,)).fetchall()
        rows = []
        for unit_id in _string_list(unit_ids, "unit_ids"):
            row = connection.execute(
                "SELECT * FROM festival_units WHERE unit_id=?", (unit_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"活动单元 {unit_id} 不存在")
            if row["site_id"] != site_id:
                raise ValidationError("方案只能包含同一站点的活动单元")
            if row["status"] not in OPEN_STATUSES:
                raise ConflictError(f"活动单元 {unit_id} 当前状态 {row['status']} 不能重复编排")
            rows.append(row)
        return rows

    def _topological_order(self, connection, units) -> list:
        by_id = {row["unit_id"]: row for row in units}
        indegree = {unit_id: 0 for unit_id in by_id}
        dependents: dict[str, list[str]] = {unit_id: [] for unit_id in by_id}
        for unit_id in by_id:
            for dep in self._unit_dependencies(connection, unit_id):
                if dep in by_id:
                    indegree[unit_id] += 1
                    dependents[dep].append(unit_id)
        ready = sorted(unit_id for unit_id, degree in indegree.items() if degree == 0)
        ordered: list = []
        while ready:
            node = ready.pop(0)
            ordered.append(by_id[node])
            for nxt in sorted(dependents[node]):
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    ready.append(nxt)
            ready.sort()
        if len(ordered) != len(by_id):
            raise ValidationError("活动单元依赖关系存在环")
        return ordered

    def generate_plan(self, *, request_id: str, actor_id: str, site_id: str,
                      unit_ids: Iterable[str] | None = None) -> dict[str, Any]:
        """结合资源现状生成可执行方案，锁定单元版本与资源版本快照。"""

        full_payload = {"actor_id": actor_id, "site_id": site_id,
                        "unit_ids": list(unit_ids) if unit_ids is not None else None}
        with self.database.transaction(immediate=True) as connection:
            actor = self.domain._actor(connection, actor_id)
            self.domain._require(actor, "admin", "operator")
            site = self._site_row(connection, site_id)
            self._check_site_scope(actor, site)

            def create() -> tuple[str, str, dict[str, Any]]:
                units = self._select_units(connection, site_id, unit_ids)
                if not units:
                    raise ValidationError("没有可编排的活动单元")
                now = self._now()
                for row in connection.execute(
                        "SELECT plan_id FROM festival_plans WHERE site_id=? AND status='draft'",
                        (site_id,)).fetchall():
                    connection.execute(
                        "UPDATE festival_plans SET status='superseded' WHERE plan_id=?",
                        (row["plan_id"],))
                    connection.execute(
                        "UPDATE festival_capacity_ledger SET state='released' "
                        "WHERE plan_id=? AND state='reserved'", (row["plan_id"],))
                plan_id = uuid.uuid4().hex
                resource_versions: dict[str, dict[str, Any]] = {}
                sign_off_items: dict[str, str] = {}
                assignments = []
                for unit in self._topological_order(connection, units):
                    blockers, staffing, refs, owners = self._evaluate_unit(
                        connection, site_id, unit, units, plan_id)
                    resource_versions.update(refs)
                    sign_off_items.update(owners)
                    assignments.append((unit, blockers, staffing))
                connection.execute(
                    "INSERT INTO festival_plans(plan_id,site_id,status,resource_versions_json,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (plan_id, site_id, "draft", canonical_json(resource_versions), actor_id, now))
                for unit, blockers, staffing in assignments:
                    connection.execute(
                        "INSERT INTO festival_plan_assignments(plan_id,unit_id,unit_version,"
                        "staffing_json,blockers_json) VALUES(?,?,?,?,?)",
                        (plan_id, unit["unit_id"], unit["version"], canonical_json(staffing),
                         canonical_json(blockers)))
                    connection.execute(
                        "UPDATE festival_units SET status='scheduled', updated_at=? "
                        "WHERE unit_id=? AND status='draft'", (now, unit["unit_id"]))
                    if not blockers:
                        connection.execute(
                            "INSERT INTO festival_capacity_ledger(entry_id,site_id,plan_id,unit_id,"
                            "slot_start,slot_end,headcount,state,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                            (uuid.uuid4().hex, site_id, plan_id, unit["unit_id"], unit["planned_start"],
                             unit["planned_end"], unit["expected_headcount"], "reserved", now))
                for check_item, responsible in sorted(sign_off_items.items()):
                    connection.execute(
                        "INSERT INTO festival_sign_offs(plan_id,check_item,responsible_actor) "
                        "VALUES(?,?,?)", (plan_id, check_item, responsible))
                blocked = {unit["unit_id"]: blockers for unit, blockers, _ in assignments if blockers}
                append_event(connection, actor_id=actor_id, action="festival.plan.generated",
                             resource_type="festival_plan", resource_id=plan_id,
                             detail={"site_id": site_id,
                                     "units": [unit["unit_id"] for unit, _, _ in assignments],
                                     "blocked_units": sorted(blocked)}, occurred_at=now)
                return "festival_plan", plan_id, {
                    "plan_id": plan_id, "status": "draft",
                    "units": [unit["unit_id"] for unit, _, _ in assignments],
                    "blocked_units": blocked,
                    "required_sign_offs": sorted(sign_off_items)}

            return self.domain.run_idempotent(connection, request_id=request_id,
                                              action="generate_festival_plan",
                                              payload=full_payload, create=create)

    def sign_off(self, *, request_id: str, actor_id: str, plan_id: str,
                 check_item: str) -> dict[str, Any]:
        """安全员签署自己负责的检查项；重复签署返回稳定结果。"""

        full_payload = {"actor_id": actor_id, "plan_id": plan_id, "check_item": check_item}
        with self.database.transaction(immediate=True) as connection:
            actor = self.domain._actor(connection, actor_id)
            self.domain._require(actor, "reviewer")
            plan = self._plan_row(connection, plan_id)
            self._check_site_scope(actor, self._site_row(connection, plan["site_id"]))
            check_item = self.domain._identifier(check_item, "check_item")

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT * FROM festival_sign_offs WHERE plan_id=? AND check_item=?",
                    (plan_id, check_item)).fetchone()
                if row is None:
                    raise NotFoundError("该检查项不在方案签署要求中")
                if row["responsible_actor"] != actor_id:
                    raise PermissionDenied("安全员只能签署自己负责的检查项")
                resource_id = f"{plan_id}/{check_item}"
                if plan["status"] != "draft":
                    raise ConflictError("方案已发布或失效，不能再签署")
                if row["signed_by"] is not None:
                    return "festival_sign_off", resource_id, {
                        "plan_id": plan_id, "check_item": check_item,
                        "signed_by": row["signed_by"], "signed_at": row["signed_at"], "noop": True}
                now = self._now()
                connection.execute(
                    "UPDATE festival_sign_offs SET signed_by=?, signed_at=? "
                    "WHERE plan_id=? AND check_item=?", (actor_id, now, plan_id, check_item))
                append_event(connection, actor_id=actor_id, action="festival.plan.signed",
                             resource_type="festival_plan", resource_id=plan_id,
                             detail={"check_item": check_item}, occurred_at=now)
                return "festival_sign_off", resource_id, {
                    "plan_id": plan_id, "check_item": check_item,
                    "signed_by": actor_id, "signed_at": now, "noop": False}

            return self.domain.run_idempotent(connection, request_id=request_id,
                                              action="sign_festival_plan",
                                              payload=full_payload, create=create)

    def release_plan(self, *, request_id: str, actor_id: str, plan_id: str) -> dict[str, Any]:
        """在签署齐备且单元与资源版本未变化时一次性发布方案。"""

        full_payload = {"actor_id": actor_id, "plan_id": plan_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self.domain._actor(connection, actor_id)
            self.domain._require(actor, "admin", "operator")
            plan = self._plan_row(connection, plan_id)
            self._check_site_scope(actor, self._site_row(connection, plan["site_id"]))

            def create() -> tuple[str, str, dict[str, Any]]:
                if plan["status"] == "released":
                    return "festival_plan", plan_id, {
                        "plan_id": plan_id, "status": "released", "noop": True}
                if plan["status"] != "draft":
                    raise ConflictError("方案已被新版本取代，不能发布")
                unsigned = [row["check_item"] for row in connection.execute(
                    "SELECT check_item FROM festival_sign_offs "
                    "WHERE plan_id=? AND signed_by IS NULL ORDER BY check_item", (plan_id,))]
                if unsigned:
                    raise ConflictError("必需签署未齐备: " + ", ".join(unsigned))
                assignments = connection.execute(
                    "SELECT * FROM festival_plan_assignments WHERE plan_id=? ORDER BY unit_id",
                    (plan_id,)).fetchall()
                units = []
                for assignment in assignments:
                    unit = self._unit_row(connection, assignment["unit_id"])
                    if unit["version"] != assignment["unit_version"]:
                        raise ConflictError(
                            f"活动单元 {unit['unit_id']} 版本已变化，请重新生成方案")
                    if unit["status"] in TERMINAL_STATUSES:
                        raise ConflictError(
                            f"活动单元 {unit['unit_id']} 已终止，不能随方案发布")
                    units.append(unit)
                snapshot = json.loads(plan["resource_versions_json"])
                changed = []
                for key, saved in sorted(snapshot.items()):
                    resource_type, resource_key = key.split(":", 1)
                    row = self._resource_row(connection, plan["site_id"],
                                             resource_type, resource_key)
                    current_version = row["version"] if row is not None else None
                    if current_version != saved["version"]:
                        changed.append(key)
                if changed:
                    raise ConflictError("资源版本已变化，请重新生成方案: " + ", ".join(changed))
                blocked: dict[str, list[dict[str, Any]]] = {}
                for unit in units:
                    unit_blockers, _, _, _ = self._evaluate_unit(
                        connection, plan["site_id"], unit, units, plan_id)
                    if unit_blockers:
                        blocked[unit["unit_id"]] = unit_blockers
                if blocked:
                    summary = "; ".join(
                        f"{unit_id}: {','.join(item['code'] for item in items)}"
                        for unit_id, items in sorted(blocked.items()))
                    raise ConflictError("方案存在阻挡条件: " + summary)
                now = self._now()
                connection.execute(
                    "UPDATE festival_plans SET status='released', released_by=?, released_at=? "
                    "WHERE plan_id=?", (actor_id, now, plan_id))
                connection.execute(
                    "UPDATE festival_units SET status='released', updated_at=? "
                    "WHERE unit_id IN (SELECT unit_id FROM festival_plan_assignments WHERE plan_id=?) "
                    "AND status IN ('draft','scheduled')", (now, plan_id))
                connection.execute(
                    "UPDATE festival_capacity_ledger SET state='committed' "
                    "WHERE plan_id=? AND state='reserved'", (plan_id,))
                append_event(connection, actor_id=actor_id, action="festival.plan.released",
                             resource_type="festival_plan", resource_id=plan_id,
                             detail={"site_id": plan["site_id"],
                                     "units": [unit["unit_id"] for unit in units]},
                             occurred_at=now)
                return "festival_plan", plan_id, {
                    "plan_id": plan_id, "status": "released",
                    "units": [unit["unit_id"] for unit in units], "noop": False}

            return self.domain.run_idempotent(connection, request_id=request_id,
                                              action="release_festival_plan",
                                              payload=full_payload, create=create)

    # ------------------------------------------------------------------
    # 单元生命周期
    # ------------------------------------------------------------------

    def transition_unit(self, *, request_id: str, actor_id: str, unit_id: str,
                        action: str) -> dict[str, Any]:
        """按明确顺序执行开始/暂停/恢复/结束/取消；重复操作保持稳定结果。"""

        if action not in TRANSITIONS:
            raise ValidationError("不支持的生命周期动作")
        full_payload = {"actor_id": actor_id, "unit_id": unit_id, "action": action}
        with self.database.transaction(immediate=True) as connection:
            actor = self.domain._actor(connection, actor_id)
            self.domain._require(actor, "admin", "operator")
            unit = self._unit_row(connection, unit_id)
            self._check_site_scope(actor, self._site_row(connection, unit["site_id"]))

            def create() -> tuple[str, str, dict[str, Any]]:
                current = unit["status"]
                targets = TRANSITIONS[action]
                if current in targets.values():
                    return "festival_unit", unit_id, {
                        "unit_id": unit_id, "action": action, "status": current, "noop": True}
                target = targets.get(current)
                if target is None:
                    raise ConflictError(f"活动单元当前状态 {current} 不允许执行 {action}")
                now = self._now()
                connection.execute(
                    "UPDATE festival_units SET status=?, updated_at=? WHERE unit_id=?",
                    (target, now, unit_id))
                if action in ("finish", "cancel"):
                    connection.execute(
                        "UPDATE festival_capacity_ledger SET state='released' "
                        "WHERE unit_id=? AND state IN ('reserved','committed')", (unit_id,))
                append_event(connection, actor_id=actor_id, action=TRANSITION_EVENTS[action],
                             resource_type="festival_unit", resource_id=unit_id,
                             detail={"from": current, "to": target}, occurred_at=now)
                return "festival_unit", unit_id, {
                    "unit_id": unit_id, "action": action, "status": target, "noop": False}

            return self.domain.run_idempotent(connection, request_id=request_id,
                                              action=f"transition_festival_unit_{action}",
                                              payload=full_payload, create=create)

    # ------------------------------------------------------------------
    # 影响分析
    # ------------------------------------------------------------------

    def report_impact(self, *, request_id: str, actor_id: str, site_id: str,
                      kind: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
        """上报降雨预警或设施变化，只对未开始单元给出迁移建议。"""

        if kind not in IMPACT_KINDS:
            raise ValidationError("影响类型不在允许范围内")
        detail = detail if detail is not None else {}
        if not isinstance(detail, dict):
            raise ValidationError("detail 必须是对象")
        full_payload = {"actor_id": actor_id, "site_id": site_id, "kind": kind, "detail": detail}
        with self.database.transaction(immediate=True) as connection:
            actor = self.domain._actor(connection, actor_id)
            self.domain._require(actor, "admin", "operator", "reviewer")
            site = self._site_row(connection, site_id)
            self._check_site_scope(actor, site)

            def create() -> tuple[str, str, dict[str, Any]]:
                impact_id = uuid.uuid4().hex
                now = self._now()
                connection.execute(
                    "INSERT INTO festival_impacts(impact_id,site_id,kind,detail_json,created_by,"
                    "created_at) VALUES(?,?,?,?,?,?)",
                    (impact_id, site_id, kind, canonical_json(detail), actor_id, now))
                units = connection.execute(
                    "SELECT * FROM festival_units WHERE site_id=? AND status IN "
                    "('scheduled','released','in_progress','paused') ORDER BY unit_id",
                    (site_id,)).fetchall()
                fallback = connection.execute(
                    "SELECT * FROM festival_resources WHERE site_id=? AND resource_type='rain_fallback' "
                    "ORDER BY resource_key LIMIT 1", (site_id,)).fetchone()
                assessments = []
                for unit in units:
                    if unit["status"] in ACTIVE_STATUSES:
                        disposition = "manual_handling"
                        suggestion: dict[str, Any] = {
                            "relocatable": False,
                            "reason": "单元正在进行，禁止静默换场，请人工处置"}
                    else:
                        disposition = "relocate_suggested"
                        if fallback is not None:
                            fallback_payload = json.loads(fallback["payload_json"])
                            suggestion = {
                                "relocatable": True,
                                "fallback_site_id": fallback_payload["fallback_site_id"],
                                "note": fallback_payload.get("note", "")}
                        else:
                            suggestion = {"relocatable": False,
                                          "reason": "站点未登记雨天备选方案"}
                    connection.execute(
                        "INSERT INTO festival_impact_assessments(impact_id,unit_id,disposition,"
                        "suggestion_json) VALUES(?,?,?,?)",
                        (impact_id, unit["unit_id"], disposition, canonical_json(suggestion)))
                    assessments.append({"unit_id": unit["unit_id"], "disposition": disposition,
                                        "suggestion": suggestion})
                append_event(connection, actor_id=actor_id, action="festival.impact.reported",
                             resource_type="festival_impact", resource_id=impact_id,
                             detail={"site_id": site_id, "kind": kind,
                                     "affected": len(assessments)}, occurred_at=now)
                return "festival_impact", impact_id, {
                    "impact_id": impact_id, "site_id": site_id, "kind": kind,
                    "assessments": assessments}

            return self.domain.run_idempotent(connection, request_id=request_id,
                                              action="report_festival_impact",
                                              payload=full_payload, create=create)

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def _to_unit(self, connection, row) -> ActivityUnit:
        return ActivityUnit(
            unit_id=row["unit_id"], site_id=row["site_id"], title=row["title"], kind=row["kind"],
            planned_start=row["planned_start"], planned_end=row["planned_end"],
            expected_headcount=row["expected_headcount"],
            required_qualifications=tuple(json.loads(row["required_qualifications_json"])),
            required_checks=tuple(json.loads(row["required_checks_json"])),
            dependencies=tuple(self._unit_dependencies(connection, row["unit_id"])),
            version=row["version"], status=row["status"],
            created_by=row["created_by"], updated_at=row["updated_at"])

    def get_unit(self, unit_id: str) -> ActivityUnit:
        connection = self.database.connection
        return self._to_unit(connection, self._unit_row(connection, unit_id))

    def list_units(self, site_id: str) -> list[ActivityUnit]:
        connection = self.database.connection
        self._site_row(connection, site_id)
        rows = connection.execute(
            "SELECT * FROM festival_units WHERE site_id=? ORDER BY unit_id", (site_id,)).fetchall()
        return [self._to_unit(connection, row) for row in rows]

    def list_resources(self, site_id: str, resource_type: str | None = None) -> list[ResourceState]:
        connection = self.database.connection
        self._site_row(connection, site_id)
        query = "SELECT * FROM festival_resources WHERE site_id=?"
        parameters: list[Any] = [site_id]
        if resource_type:
            query += " AND resource_type=?"
            parameters.append(resource_type)
        query += " ORDER BY resource_type, resource_key"
        return [ResourceState(site_id=row["site_id"], resource_type=row["resource_type"],
                              resource_key=row["resource_key"],
                              payload=json.loads(row["payload_json"]), version=row["version"],
                              maintained_by=row["maintained_by"], updated_at=row["updated_at"])
                for row in connection.execute(query, parameters)]

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        connection = self.database.connection
        plan = self._plan_row(connection, plan_id)
        assignments = []
        for row in connection.execute(
                "SELECT a.*, u.title, u.status AS unit_status, u.version AS current_version "
                "FROM festival_plan_assignments a JOIN festival_units u ON u.unit_id=a.unit_id "
                "WHERE a.plan_id=? ORDER BY a.unit_id", (plan_id,)):
            assignments.append({
                "unit_id": row["unit_id"], "title": row["title"], "unit_status": row["unit_status"],
                "locked_version": row["unit_version"], "current_version": row["current_version"],
                "staffing": json.loads(row["staffing_json"]),
                "blockers": json.loads(row["blockers_json"])})
        sign_offs = [SignOff(plan_id=row["plan_id"], check_item=row["check_item"],
                             responsible_actor=row["responsible_actor"],
                             signed_by=row["signed_by"], signed_at=row["signed_at"]).__dict__
                     for row in connection.execute(
                         "SELECT * FROM festival_sign_offs WHERE plan_id=? ORDER BY check_item",
                         (plan_id,))]
        return {"plan_id": plan["plan_id"], "site_id": plan["site_id"], "status": plan["status"],
                "created_by": plan["created_by"], "created_at": plan["created_at"],
                "released_by": plan["released_by"], "released_at": plan["released_at"],
                "resource_versions": json.loads(plan["resource_versions_json"]),
                "assignments": assignments, "sign_offs": sign_offs}

    def list_plans(self, site_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        self._site_row(connection, site_id)
        return [{"plan_id": row["plan_id"], "status": row["status"],
                 "created_by": row["created_by"], "created_at": row["created_at"],
                 "released_by": row["released_by"], "released_at": row["released_at"]}
                for row in connection.execute(
                    "SELECT * FROM festival_plans WHERE site_id=? ORDER BY created_at, plan_id",
                    (site_id,))]

    def clearance(self, site_id: str) -> dict[str, Any]:
        """实时评估站点内未终止单元的放行状态与阻挡条件。"""

        connection = self.database.connection
        self._site_row(connection, site_id)
        rows = connection.execute(
            "SELECT * FROM festival_units WHERE site_id=? AND status NOT IN ('finished','cancelled') "
            "ORDER BY unit_id", (site_id,)).fetchall()
        batch = [row for row in rows if row["status"] in OPEN_STATUSES]
        items = []
        for row in rows:
            if row["status"] in OPEN_STATUSES:
                blockers, _, _, _ = self._evaluate_unit(connection, site_id, row, batch, None)
                items.append({"unit_id": row["unit_id"], "title": row["title"],
                              "status": row["status"], "clearable": not blockers,
                              "blockers": blockers})
            else:
                items.append({"unit_id": row["unit_id"], "title": row["title"],
                              "status": row["status"], "clearable": True, "blockers": []})
        return {"site_id": site_id, "items": items}

    def capacity_view(self, site_id: str) -> dict[str, Any]:
        """展示站点人流上限与账本占用的时间峰值。"""

        connection = self.database.connection
        self._site_row(connection, site_id)
        limit_row = self._resource_row(connection, site_id, "capacity_limit", site_id)
        limit = (json.loads(limit_row["payload_json"])["max_headcount"]
                 if limit_row is not None else None)
        entries = [CapacityEntry(entry_id=row["entry_id"], site_id=row["site_id"],
                                 plan_id=row["plan_id"], unit_id=row["unit_id"],
                                 slot_start=row["slot_start"], slot_end=row["slot_end"],
                                 headcount=row["headcount"], state=row["state"])
                   for row in connection.execute(
                       "SELECT * FROM festival_capacity_ledger WHERE site_id=? AND state!='released' "
                       "ORDER BY slot_start, entry_id", (site_id,))]
        events: list[tuple[str, int]] = []
        for entry in entries:
            events.append((entry.slot_start, entry.headcount))
            events.append((entry.slot_end, -entry.headcount))
        events.sort()
        peak = current = 0
        for _, delta in events:
            current += delta
            peak = max(peak, current)
        return {"site_id": site_id, "max_headcount": limit, "peak_reserved": peak,
                "entries": [entry.__dict__ for entry in entries]}

    def responsibility_chain(self, plan_id: str) -> dict[str, Any]:
        """展示方案调整前后的责任链：提交人、签署人、资源维护人及其版本变化。"""

        connection = self.database.connection
        plan = self._plan_row(connection, plan_id)
        snapshot = json.loads(plan["resource_versions_json"])
        before, after, changes = [], [], []
        for key, saved in sorted(snapshot.items()):
            resource_type, resource_key = key.split(":", 1)
            before.append({"resource_type": resource_type, "resource_key": resource_key,
                           "version": saved["version"], "maintained_by": saved["maintained_by"]})
            row = self._resource_row(connection, plan["site_id"], resource_type, resource_key)
            current = {"resource_type": resource_type, "resource_key": resource_key,
                       "version": row["version"] if row is not None else None,
                       "maintained_by": row["maintained_by"] if row is not None else None}
            after.append(current)
            if current["version"] != saved["version"]:
                changes.append({"resource_type": resource_type, "resource_key": resource_key,
                                "before": {"version": saved["version"],
                                           "maintained_by": saved["maintained_by"]},
                                "after": current})
        units = [{"unit_id": row["unit_id"], "title": row["title"], "created_by": row["created_by"],
                  "locked_version": row["unit_version"], "current_version": row["version"]}
                 for row in connection.execute(
                     "SELECT u.unit_id, u.title, u.created_by, u.version, a.unit_version "
                     "FROM festival_plan_assignments a JOIN festival_units u ON u.unit_id=a.unit_id "
                     "WHERE a.plan_id=? ORDER BY a.unit_id", (plan_id,))]
        sign_offs = [{"check_item": row["check_item"],
                      "responsible_actor": row["responsible_actor"],
                      "signed_by": row["signed_by"], "signed_at": row["signed_at"]}
                     for row in connection.execute(
                         "SELECT * FROM festival_sign_offs WHERE plan_id=? ORDER BY check_item",
                         (plan_id,))]
        return {"plan_id": plan["plan_id"], "site_id": plan["site_id"], "status": plan["status"],
                "created_by": plan["created_by"], "created_at": plan["created_at"],
                "released_by": plan["released_by"], "released_at": plan["released_at"],
                "units": units, "sign_offs": sign_offs,
                "resources_before": before, "resources_after": after,
                "resource_changes": changes}

    def get_impact(self, impact_id: str) -> dict[str, Any]:
        connection = self.database.connection
        row = connection.execute(
            "SELECT * FROM festival_impacts WHERE impact_id=?", (impact_id,)).fetchone()
        if row is None:
            raise NotFoundError("影响事件不存在")
        assessments = [ImpactAssessment(
            impact_id=item["impact_id"], unit_id=item["unit_id"],
            disposition=item["disposition"],
            suggestion=json.loads(item["suggestion_json"])).__dict__
            for item in connection.execute(
                "SELECT * FROM festival_impact_assessments WHERE impact_id=? ORDER BY unit_id",
                (impact_id,))]
        return {"impact_id": row["impact_id"], "site_id": row["site_id"], "kind": row["kind"],
                "detail": json.loads(row["detail_json"]), "created_by": row["created_by"],
                "created_at": row["created_at"], "assessments": assessments}

    def list_impacts(self, site_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        self._site_row(connection, site_id)
        return [{"impact_id": row["impact_id"], "kind": row["kind"],
                 "created_by": row["created_by"], "created_at": row["created_at"]}
                for row in connection.execute(
                    "SELECT * FROM festival_impacts WHERE site_id=? ORDER BY created_at, impact_id",
                    (site_id,))]
