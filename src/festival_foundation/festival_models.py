"""定义中秋活动编排领域在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ActivityUnit:
    """表示一个带版本、可编排的活动单元。"""

    unit_id: str
    site_id: str
    title: str
    kind: str
    planned_start: str
    planned_end: str
    expected_headcount: int
    required_qualifications: tuple[str, ...]
    required_checks: tuple[str, ...]
    dependencies: tuple[str, ...]
    version: int
    status: str
    created_by: str
    updated_at: str


@dataclass(frozen=True)
class ResourceState:
    """表示一项由指定人员维护、带版本的保障资源。"""

    site_id: str
    resource_type: str
    resource_key: str
    payload: dict[str, Any]
    version: int
    maintained_by: str
    updated_at: str


@dataclass(frozen=True)
class FestivalPlan:
    """表示一份可执行方案及其发布状态。"""

    plan_id: str
    site_id: str
    status: str
    resource_versions: dict[str, dict[str, Any]]
    created_by: str
    created_at: str
    released_by: str | None
    released_at: str | None


@dataclass(frozen=True)
class PlanAssignment:
    """表示方案中一个活动单元的排定结果。"""

    plan_id: str
    unit_id: str
    unit_version: int
    staffing: dict[str, str]
    blockers: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class SignOff:
    """表示方案中一个检查项的签署状态。"""

    plan_id: str
    check_item: str
    responsible_actor: str
    signed_by: str | None
    signed_at: str | None


@dataclass(frozen=True)
class CapacityEntry:
    """表示容量账本中的一条占用记录。"""

    entry_id: str
    site_id: str
    plan_id: str
    unit_id: str
    slot_start: str
    slot_end: str
    headcount: int
    state: str


@dataclass(frozen=True)
class ImpactAssessment:
    """表示影响事件对一个活动单元的处置结论。"""

    impact_id: str
    unit_id: str
    disposition: str
    suggestion: dict[str, Any]
