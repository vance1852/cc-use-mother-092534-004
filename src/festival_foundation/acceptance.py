"""运行基础服务与中秋活动放行中枢的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .orchestration import OrchestrationService
from .storage import Database


def run() -> dict[str, object]:
    """执行从基础登记到降雨迁移再发布的完整链路并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        service = OrchestrationService(
            database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范服务机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-planner", actor_id="admin-001",
                               new_actor_id="planner-001", display_name="活动策划人",
                               role="operator", organization_id="org-001")
        service.register_actor(request_id="req-fire", actor_id="admin-001",
                               new_actor_id="fire-001", display_name="消防安全员",
                               role="reviewer", organization_id="org-001")
        service.register_actor(request_id="req-material", actor_id="admin-001",
                               new_actor_id="material-001", display_name="物资安全员",
                               role="reviewer", organization_id="org-001")
        service.register_actor(request_id="req-volunteer", actor_id="admin-001",
                               new_actor_id="volunteer-001", display_name="现场志愿者",
                               role="operator", organization_id="org-001")
        service.register_site(request_id="req-site-plaza", actor_id="planner-001",
                              site_id="site-plaza", organization_id="org-001",
                              name="中心广场", timezone_name="Asia/Shanghai")
        service.register_site(request_id="req-site-hall", actor_id="planner-001",
                              site_id="site-hall", organization_id="org-001",
                              name="文化礼堂", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="planner-001",
                                           site_id="site-plaza", category="organization_profile",
                                           external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="planner-001",
                                            site_id="site-plaza", category="organization_profile",
                                            external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})

        service.configure_site_resource(request_id="req-res-plaza", actor_id="planner-001",
                                        site_id="site-plaza", max_headcount=300, indoor=False)
        service.configure_site_resource(request_id="req-res-hall", actor_id="planner-001",
                                        site_id="site-hall", max_headcount=400, indoor=True)
        service.upsert_site_window(request_id="req-win-plaza", actor_id="planner-001",
                                   window_id="window-plaza", site_id="site-plaza",
                                   window_start="2026-09-26T10:00:00+08:00",
                                   window_end="2026-09-26T23:00:00+08:00")
        service.upsert_site_window(request_id="req-win-hall", actor_id="planner-001",
                                   window_id="window-hall", site_id="site-hall",
                                   window_start="2026-09-26T10:00:00+08:00",
                                   window_end="2026-09-26T23:00:00+08:00")
        service.grant_certification(request_id="req-cert", actor_id="admin-001",
                                    target_actor_id="volunteer-001", cert_code="volunteer_basic")
        service.assign_safety_check(request_id="req-fire-plaza", actor_id="planner-001",
                                    check_id="fire-plaza", scope_type="site",
                                    scope_key="site-plaza", check_kind="fire",
                                    title="广场消防检查", owner_actor_id="fire-001")
        service.assign_safety_check(request_id="req-fire-hall", actor_id="planner-001",
                                    check_id="fire-hall", scope_type="site",
                                    scope_key="site-hall", check_kind="fire",
                                    title="礼堂消防检查", owner_actor_id="fire-001")
        service.assign_safety_check(request_id="req-mat-lantern", actor_id="planner-001",
                                    check_id="material-lantern", scope_type="unit",
                                    scope_key="lantern", check_kind="material",
                                    title="花灯用电检查", owner_actor_id="material-001")

        post = [{"post": "现场引导岗", "required_cert": "volunteer_basic",
                 "actor_id": "volunteer-001"}]
        units = [
            {"key": "poetry", "name": "中秋诗会", "site_id": "site-plaza",
             "start_time": "2026-09-26T18:00:00+08:00", "end_time": "2026-09-26T19:00:00+08:00",
             "expected_headcount": 120, "posts": post, "depends_on": [],
             "rain_backup_site_id": "site-hall"},
            {"key": "lantern", "name": "花灯展示", "site_id": "site-plaza",
             "start_time": "2026-09-26T19:00:00+08:00", "end_time": "2026-09-26T20:30:00+08:00",
             "expected_headcount": 150, "posts": post, "depends_on": ["poetry"],
             "rain_backup_site_id": "site-hall"},
            {"key": "folklore", "name": "民俗体验", "site_id": "site-plaza",
             "start_time": "2026-09-26T20:30:00+08:00", "end_time": "2026-09-26T22:00:00+08:00",
             "expected_headcount": 100, "posts": post, "depends_on": ["lantern"],
             "rain_backup_site_id": "site-hall"},
        ]
        service.submit_plan(request_id="req-plan", actor_id="planner-001", plan_id="plan-midautumn",
                            name="社区中秋晚会", units=units)
        blocked_gate = service.get_plan_gate("plan-midautumn")
        gate_blocked = not blocked_gate["releasable"]
        for check in blocked_gate["required_checks"]:
            service.sign_check(request_id=f"req-sign-{check['check_id']}",
                               actor_id=check["owner_actor_id"], plan_id="plan-midautumn",
                               check_id=check["check_id"])
        first_publish = service.publish_plan(request_id="req-publish", actor_id="planner-001",
                                             plan_id="plan-midautumn")

        # 诗会开始后突降降雨：诗会进入人工处置，其余单元获得迁移建议。
        service.start_unit(request_id="req-start-poetry", actor_id="planner-001",
                           plan_id="plan-midautumn", unit_key="poetry")
        rain = service.declare_rain_alert(request_id="req-rain", actor_id="planner-001",
                                          incident_id="incident-rain")
        impact = service.analyze_impact("plan-midautumn")
        service.resolve_manual_unit(request_id="req-resolve-poetry", actor_id="fire-001",
                                    plan_id="plan-midautumn", unit_key="poetry", decision="end")
        service.accept_relocation(request_id="req-accept-lantern", actor_id="planner-001",
                                  plan_id="plan-midautumn", unit_key="lantern")
        service.accept_relocation(request_id="req-accept-folklore", actor_id="planner-001",
                                  plan_id="plan-midautumn", unit_key="folklore")
        draft_gate = service.get_plan_gate("plan-midautumn")
        for check in draft_gate["required_checks"]:
            if check["status"] in ("missing", "stale"):
                service.sign_check(request_id=f"req-resign-{check['check_id']}",
                                   actor_id=check["owner_actor_id"], plan_id="plan-midautumn",
                                   check_id=check["check_id"])
        second_publish = service.publish_plan(request_id="req-republish", actor_id="planner-001",
                                              plan_id="plan-midautumn")
        service.start_unit(request_id="req-start-folklore", actor_id="planner-001",
                           plan_id="plan-midautumn", unit_key="folklore")
        service.end_unit(request_id="req-end-folklore", actor_id="planner-001",
                         plan_id="plan-midautumn", unit_key="folklore")

        board = service.release_board()
        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-plaza")
        result = {
            "status": "ok",
            "records": len(records),
            "audit_events": event_count,
            "audit_valid": valid,
            "first_replayed": first.replayed,
            "second_replayed": replay.replayed,
            "gate_blocked_before_signoff": gate_blocked,
            "first_revision": first_publish["revision_no"],
            "rain_manual_units": [u["unit_key"] for u in rain["manual_units"]],
            "rain_suggestions": [s["unit_key"] for s in rain["suggestions"]],
            "impact_manual_count": len(impact["manual_units"]),
            "second_revision": second_publish["revision_no"],
            "relocated_sites": {key: unit["site_id"]
                                for key, unit in second_publish["gate"]["units"].items()},
            "board_states": {item["unit_key"]: item["state"] for item in board["items"]},
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
