"""运行中秋活动编排与安全放行的离线端到端验收。

在临时 SQLite 数据库中完成：建档、资源维护、单元提交、方案生成、
安全签署、一次性发布、生命周期流转与降雨影响分析，核对审计链后
输出一行 `status` 为 `ok` 的 JSON。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .festival import FestivalService
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    """执行一条完整编排放行链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "festival_acceptance.sqlite3")
        domain = DomainService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        festival = FestivalService(domain)

        domain.register_organization(request_id="req-org", actor_id="bootstrap",
                                     organization_id="org-001", name="社区文化活动中心")
        domain.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                              display_name="系统管理员", role="admin", organization_id="org-001")
        domain.register_actor(request_id="req-planner", actor_id="admin-001", new_actor_id="planner-001",
                              display_name="活动策划人", role="operator", organization_id="org-001")
        domain.register_actor(request_id="req-staff", actor_id="admin-001", new_actor_id="staff-001",
                              display_name="现场志愿者", role="operator", organization_id="org-001")
        domain.register_actor(request_id="req-safety-1", actor_id="admin-001", new_actor_id="safety-001",
                              display_name="消防安全员", role="reviewer", organization_id="org-001")
        domain.register_actor(request_id="req-safety-2", actor_id="admin-001", new_actor_id="safety-002",
                              display_name="用电安全员", role="reviewer", organization_id="org-001")
        domain.register_site(request_id="req-site", actor_id="planner-001", site_id="site-001",
                             organization_id="org-001", name="中心广场", timezone_name="Asia/Shanghai")
        domain.register_site(request_id="req-site-indoor", actor_id="planner-001", site_id="site-002",
                             organization_id="org-001", name="室内礼堂", timezone_name="Asia/Shanghai")

        festival.update_resource(request_id="req-window", actor_id="planner-001", site_id="site-001",
                                 resource_type="site_window", resource_key="site-001",
                                 payload={"windows": [{"start": "2026-09-25T10:00:00Z",
                                                       "end": "2026-09-25T16:00:00Z"}]})
        festival.update_resource(request_id="req-capacity", actor_id="planner-001", site_id="site-001",
                                 resource_type="capacity_limit", resource_key="site-001",
                                 payload={"max_headcount": 500})
        festival.update_resource(request_id="req-qualification", actor_id="planner-001", site_id="site-001",
                                 resource_type="qualification", resource_key="staff-001",
                                 payload={"qualifications": ["crowd_control"]})
        festival.update_resource(request_id="req-check-fire", actor_id="planner-001", site_id="site-001",
                                 resource_type="material_check", resource_key="fire_check",
                                 payload={"responsible_actor": "safety-001",
                                          "description": "消防通道与器材检查"})
        festival.update_resource(request_id="req-check-power", actor_id="planner-001", site_id="site-001",
                                 resource_type="material_check", resource_key="lantern_power",
                                 payload={"responsible_actor": "safety-002",
                                          "description": "花灯用电线路检查"})
        festival.update_resource(request_id="req-fallback", actor_id="planner-001", site_id="site-001",
                                 resource_type="rain_fallback", resource_key="site-001",
                                 payload={"fallback_site_id": "site-002",
                                          "note": "降雨时迁入室内礼堂"})

        festival.submit_unit(request_id="req-unit-poetry", actor_id="planner-001",
                             unit_id="unit-poetry", site_id="site-001", title="中秋诗会",
                             kind="poetry", planned_start="2026-09-25T11:00:00Z",
                             planned_end="2026-09-25T12:00:00Z", expected_headcount=100,
                             required_qualifications=["crowd_control"],
                             required_checks=["fire_check"])
        festival.submit_unit(request_id="req-unit-lantern", actor_id="planner-001",
                             unit_id="unit-lantern", site_id="site-001", title="花灯展示",
                             kind="lantern", planned_start="2026-09-25T12:00:00Z",
                             planned_end="2026-09-25T13:30:00Z", expected_headcount=200,
                             required_qualifications=["crowd_control"],
                             required_checks=["fire_check", "lantern_power"])
        festival.submit_unit(request_id="req-unit-folk", actor_id="planner-001",
                             unit_id="unit-folk", site_id="site-001", title="民俗体验",
                             kind="folk", planned_start="2026-09-25T12:30:00Z",
                             planned_end="2026-09-25T13:30:00Z", expected_headcount=150,
                             required_qualifications=["crowd_control"],
                             required_checks=["fire_check"], dependencies=["unit-poetry"])

        plan = festival.generate_plan(request_id="req-plan", actor_id="planner-001", site_id="site-001")
        plan_id = plan["plan_id"]
        festival.sign_off(request_id="req-sign-fire", actor_id="safety-001",
                          plan_id=plan_id, check_item="fire_check")
        festival.sign_off(request_id="req-sign-power", actor_id="safety-002",
                          plan_id=plan_id, check_item="lantern_power")
        release = festival.release_plan(request_id="req-release", actor_id="planner-001",
                                        plan_id=plan_id)

        festival.transition_unit(request_id="req-start", actor_id="planner-001",
                                 unit_id="unit-poetry", action="start")
        festival.transition_unit(request_id="req-pause", actor_id="planner-001",
                                 unit_id="unit-poetry", action="pause")
        festival.transition_unit(request_id="req-resume", actor_id="planner-001",
                                 unit_id="unit-poetry", action="resume")
        festival.transition_unit(request_id="req-finish", actor_id="planner-001",
                                 unit_id="unit-poetry", action="finish")

        impact = festival.report_impact(request_id="req-impact", actor_id="safety-001",
                                        site_id="site-001", kind="rain_alert",
                                        detail={"source": "气象预警", "level": "orange"})

        clearance = festival.clearance("site-001")
        chain = festival.responsibility_chain(plan_id)
        capacity = festival.capacity_view("site-001")
        valid, event_count = domain.verify_audit()
        ok = (not plan["blocked_units"] and release["status"] == "released"
              and all(item["disposition"] == "relocate_suggested" for item in impact["assessments"])
              and valid)
        result = {"status": "ok" if ok else "failed",
                  "plan_id": plan_id, "plan_status": release["status"],
                  "blocked_units": len(plan["blocked_units"]),
                  "impact_assessments": len(impact["assessments"]),
                  "clearance_items": len(clearance["items"]),
                  "resource_changes": len(chain["resource_changes"]),
                  "capacity_peak": capacity["peak_reserved"],
                  "audit_events": event_count, "audit_valid": valid}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
