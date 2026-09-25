import unittest
from datetime import datetime, timezone

from festival_foundation.clock import FixedClock
from festival_foundation.errors import (ConflictError, NotFoundError, PermissionDenied,
                                        ValidationError)
from festival_foundation.festival import FestivalService
from festival_foundation.service import DomainService
from festival_foundation.storage import Database


class FestivalServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.domain = DomainService(self.database, FixedClock(datetime(2026, 9, 25, tzinfo=timezone.utc)))
        self.festival = FestivalService(self.domain)
        self.domain.register_organization(request_id="org", actor_id="bootstrap",
                                          organization_id="o1", name="社区文化中心")
        self.domain.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                   display_name="管理员", role="admin", organization_id="o1")
        self.domain.register_actor(request_id="planner", actor_id="a1", new_actor_id="op1",
                                   display_name="策划人", role="operator", organization_id="o1")
        self.domain.register_actor(request_id="staff", actor_id="a1", new_actor_id="st1",
                                   display_name="志愿者", role="operator", organization_id="o1")
        self.domain.register_actor(request_id="safety1", actor_id="a1", new_actor_id="rv1",
                                   display_name="消防安全员", role="reviewer", organization_id="o1")
        self.domain.register_actor(request_id="safety2", actor_id="a1", new_actor_id="rv2",
                                   display_name="用电安全员", role="reviewer", organization_id="o1")
        self.domain.register_site(request_id="site", actor_id="op1", site_id="s1",
                                  organization_id="o1", name="中心广场", timezone_name="Asia/Shanghai")
        self.domain.register_site(request_id="site2", actor_id="op1", site_id="s2",
                                  organization_id="o1", name="室内礼堂", timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    # --------------------------------------------------------------
    # 准备工具
    # --------------------------------------------------------------

    def _resources(self, capacity=500):
        self.festival.update_resource(
            request_id="r-window", actor_id="op1", site_id="s1",
            resource_type="site_window", resource_key="s1",
            payload={"windows": [{"start": "2026-09-25T10:00:00Z", "end": "2026-09-25T16:00:00Z"}]})
        self.festival.update_resource(
            request_id="r-capacity", actor_id="op1", site_id="s1",
            resource_type="capacity_limit", resource_key="s1",
            payload={"max_headcount": capacity})
        self.festival.update_resource(
            request_id="r-qual", actor_id="op1", site_id="s1",
            resource_type="qualification", resource_key="st1",
            payload={"qualifications": ["crowd_control"]})
        self.festival.update_resource(
            request_id="r-fire", actor_id="op1", site_id="s1",
            resource_type="material_check", resource_key="fire_check",
            payload={"responsible_actor": "rv1", "description": "消防检查"})
        self.festival.update_resource(
            request_id="r-power", actor_id="op1", site_id="s1",
            resource_type="material_check", resource_key="lantern_power",
            payload={"responsible_actor": "rv2", "description": "用电检查"})
        self.festival.update_resource(
            request_id="r-fallback", actor_id="op1", site_id="s1",
            resource_type="rain_fallback", resource_key="s1",
            payload={"fallback_site_id": "s2", "note": "迁入室内礼堂"})

    def _unit(self, request_id, unit_id, start, end, headcount=100,
              checks=("fire_check",), deps=()):
        return self.festival.submit_unit(
            request_id=request_id, actor_id="op1", unit_id=unit_id, site_id="s1",
            title=f"活动{unit_id}", kind="poetry", planned_start=start, planned_end=end,
            expected_headcount=headcount, required_qualifications=["crowd_control"],
            required_checks=list(checks), dependencies=list(deps))

    def _standard_units(self):
        self._unit("u-poetry", "poetry", "2026-09-25T11:00:00Z", "2026-09-25T12:00:00Z", 100)
        self._unit("u-lantern", "lantern", "2026-09-25T12:00:00Z", "2026-09-25T13:30:00Z", 200,
                   checks=("fire_check", "lantern_power"))
        self._unit("u-folk", "folk", "2026-09-25T12:30:00Z", "2026-09-25T13:30:00Z", 150,
                   deps=("poetry",))

    def _released_plan(self, request_id="p1"):
        self._resources()
        self._standard_units()
        plan = self.festival.generate_plan(request_id=request_id, actor_id="op1", site_id="s1")
        plan_id = plan["plan_id"]
        self.festival.sign_off(request_id=f"{request_id}-sign1", actor_id="rv1",
                               plan_id=plan_id, check_item="fire_check")
        self.festival.sign_off(request_id=f"{request_id}-sign2", actor_id="rv2",
                               plan_id=plan_id, check_item="lantern_power")
        self.festival.release_plan(request_id=f"{request_id}-release", actor_id="op1",
                                   plan_id=plan_id)
        return plan_id

    # --------------------------------------------------------------
    # 单元与依赖
    # --------------------------------------------------------------

    def test_submit_unit_tracks_version_and_dependencies(self):
        first = self._unit("u1", "poetry", "2026-09-25T11:00:00Z", "2026-09-25T12:00:00Z")
        self.assertEqual(1, first["version"])
        revised = self._unit("u2", "poetry", "2026-09-25T11:00:00Z", "2026-09-25T12:30:00Z")
        self.assertEqual(2, revised["version"])
        unit = self.festival.get_unit("poetry")
        self.assertEqual("2026-09-25T12:30:00Z", unit.planned_end)

    def test_submit_same_content_keeps_version(self):
        self._unit("u1", "poetry", "2026-09-25T11:00:00Z", "2026-09-25T12:00:00Z")
        again = self._unit("u2", "poetry", "2026-09-25T11:00:00Z", "2026-09-25T12:00:00Z")
        self.assertEqual(1, again["version"])
        self.assertTrue(again["noop"])

    def test_dependency_cycle_is_rejected(self):
        self._unit("u1", "poetry", "2026-09-25T11:00:00Z", "2026-09-25T12:00:00Z")
        self._unit("u2", "folk", "2026-09-25T12:00:00Z", "2026-09-25T13:00:00Z", deps=("poetry",))
        with self.assertRaises(ValidationError):
            self.festival.submit_unit(
                request_id="u3", actor_id="op1", unit_id="poetry", site_id="s1", title="诗会",
                kind="poetry", planned_start="2026-09-25T11:00:00Z",
                planned_end="2026-09-25T12:00:00Z", expected_headcount=100,
                required_qualifications=[], required_checks=[], dependencies=["folk"])

    def test_dependency_must_exist_in_same_site(self):
        with self.assertRaises(NotFoundError):
            self._unit("u1", "folk", "2026-09-25T12:00:00Z", "2026-09-25T13:00:00Z",
                       deps=("missing",))

    def test_released_unit_cannot_be_revised(self):
        self._released_plan()
        with self.assertRaises(ConflictError):
            self._unit("u-revise", "poetry", "2026-09-25T11:00:00Z", "2026-09-25T12:30:00Z")

    # --------------------------------------------------------------
    # 资源维护
    # --------------------------------------------------------------

    def test_resource_version_increases_only_on_change(self):
        self._resources()
        same = self.festival.update_resource(
            request_id="r-capacity-2", actor_id="op1", site_id="s1",
            resource_type="capacity_limit", resource_key="s1",
            payload={"max_headcount": 500})
        self.assertEqual(1, same["version"])
        changed = self.festival.update_resource(
            request_id="r-capacity-3", actor_id="op1", site_id="s1",
            resource_type="capacity_limit", resource_key="s1",
            payload={"max_headcount": 300})
        self.assertEqual(2, changed["version"])
        self.assertEqual("op1", changed["maintained_by"])

    def test_material_check_requires_active_reviewer(self):
        with self.assertRaises(ValidationError):
            self.festival.update_resource(
                request_id="r-bad", actor_id="op1", site_id="s1",
                resource_type="material_check", resource_key="fire_check",
                payload={"responsible_actor": "st1"})

    def test_operator_cannot_touch_other_organization_site(self):
        self.domain.register_organization(request_id="org2", actor_id="a1",
                                          organization_id="o2", name="其他机构")
        self.domain.register_actor(request_id="op2", actor_id="a1", new_actor_id="op2",
                                   display_name="外部策划", role="operator", organization_id="o2")
        with self.assertRaises(PermissionDenied):
            self.festival.submit_unit(
                request_id="ux", actor_id="op2", unit_id="x1", site_id="s1", title="越权",
                kind="other", planned_start="2026-09-25T11:00:00Z",
                planned_end="2026-09-25T12:00:00Z", expected_headcount=10,
                required_qualifications=[], required_checks=[])

    # --------------------------------------------------------------
    # 方案生成与阻挡条件
    # --------------------------------------------------------------

    def test_generate_plan_collects_sign_offs_and_reserves_capacity(self):
        self._resources()
        self._standard_units()
        plan = self.festival.generate_plan(request_id="p1", actor_id="op1", site_id="s1")
        self.assertEqual({}, plan["blocked_units"])
        self.assertEqual(["fire_check", "lantern_power"], plan["required_sign_offs"])
        capacity = self.festival.capacity_view("s1")
        self.assertEqual(500, capacity["max_headcount"])
        self.assertEqual(350, capacity["peak_reserved"])
        units = {unit.unit_id: unit.status for unit in self.festival.list_units("s1")}
        self.assertEqual({"poetry": "scheduled", "lantern": "scheduled", "folk": "scheduled"}, units)

    def test_missing_resources_block_clearance(self):
        self._unit("u1", "poetry", "2026-09-25T11:00:00Z", "2026-09-25T12:00:00Z")
        clearance = self.festival.clearance("s1")
        codes = {item["code"] for item in clearance["items"][0]["blockers"]}
        self.assertIn("missing_site_window", codes)
        self.assertIn("missing_capacity_limit", codes)
        self.assertIn("missing_qualification", codes)
        self.assertIn("missing_check_item", codes)
        self.assertFalse(clearance["items"][0]["clearable"])

    def test_outside_window_and_capacity_overflow_are_blocked(self):
        self._resources(capacity=150)
        self._unit("u1", "early", "2026-09-25T09:00:00Z", "2026-09-25T10:30:00Z", 50)
        self._unit("u2", "big-a", "2026-09-25T11:00:00Z", "2026-09-25T12:00:00Z", 100)
        self._unit("u3", "big-b", "2026-09-25T11:30:00Z", "2026-09-25T12:30:00Z", 100)
        clearance = self.festival.clearance("s1")
        by_id = {item["unit_id"]: item for item in clearance["items"]}
        self.assertIn("outside_open_window", {b["code"] for b in by_id["early"]["blockers"]})
        self.assertIn("capacity_exceeded", {b["code"] for b in by_id["big-a"]["blockers"]})
        self.assertIn("capacity_exceeded", {b["code"] for b in by_id["big-b"]["blockers"]})

    def test_dependency_order_violation_blocks(self):
        self._resources()
        self._unit("u1", "poetry", "2026-09-25T11:00:00Z", "2026-09-25T12:00:00Z")
        self._unit("u2", "folk", "2026-09-25T11:30:00Z", "2026-09-25T12:30:00Z", deps=("poetry",))
        clearance = self.festival.clearance("s1")
        folk = next(item for item in clearance["items"] if item["unit_id"] == "folk")
        self.assertIn("dependency_order", {b["code"] for b in folk["blockers"]})

    def test_regenerating_plan_supersedes_previous_draft(self):
        self._resources()
        self._standard_units()
        first = self.festival.generate_plan(request_id="p1", actor_id="op1", site_id="s1")
        second = self.festival.generate_plan(request_id="p2", actor_id="op1", site_id="s1")
        plans = {item["plan_id"]: item["status"] for item in self.festival.list_plans("s1")}
        self.assertEqual("superseded", plans[first["plan_id"]])
        self.assertEqual("draft", plans[second["plan_id"]])
        capacity = self.festival.capacity_view("s1")
        self.assertEqual(350, capacity["peak_reserved"])

    # --------------------------------------------------------------
    # 签署与发布
    # --------------------------------------------------------------

    def test_sign_off_requires_responsible_reviewer(self):
        self._resources()
        self._standard_units()
        plan = self.festival.generate_plan(request_id="p1", actor_id="op1", site_id="s1")
        plan_id = plan["plan_id"]
        with self.assertRaises(PermissionDenied):
            self.festival.sign_off(request_id="s-wrong-role", actor_id="op1",
                                   plan_id=plan_id, check_item="fire_check")
        with self.assertRaises(PermissionDenied):
            self.festival.sign_off(request_id="s-wrong-owner", actor_id="rv2",
                                   plan_id=plan_id, check_item="fire_check")
        with self.assertRaises(NotFoundError):
            self.festival.sign_off(request_id="s-unknown", actor_id="rv1",
                                   plan_id=plan_id, check_item="unknown_check")
        signed = self.festival.sign_off(request_id="s-ok", actor_id="rv1",
                                        plan_id=plan_id, check_item="fire_check")
        self.assertEqual("rv1", signed["signed_by"])
        replay = self.festival.sign_off(request_id="s-again", actor_id="rv1",
                                        plan_id=plan_id, check_item="fire_check")
        self.assertTrue(replay["noop"])

    def test_release_requires_all_sign_offs(self):
        self._resources()
        self._standard_units()
        plan = self.festival.generate_plan(request_id="p1", actor_id="op1", site_id="s1")
        self.festival.sign_off(request_id="s1-sign", actor_id="rv1",
                               plan_id=plan["plan_id"], check_item="fire_check")
        with self.assertRaises(ConflictError):
            self.festival.release_plan(request_id="rel", actor_id="op1", plan_id=plan["plan_id"])

    def test_release_is_atomic_and_repeatable(self):
        plan_id = self._released_plan()
        units = {unit.unit_id: unit.status for unit in self.festival.list_units("s1")}
        self.assertEqual({"poetry": "released", "lantern": "released", "folk": "released"}, units)
        capacity = self.festival.capacity_view("s1")
        self.assertTrue(all(entry["state"] == "committed" for entry in capacity["entries"]))
        again = self.festival.release_plan(request_id="rel-again", actor_id="op1", plan_id=plan_id)
        self.assertTrue(again["noop"])
        self.assertEqual("released", again["status"])

    def test_resource_change_after_generation_blocks_release(self):
        self._resources()
        self._standard_units()
        plan = self.festival.generate_plan(request_id="p1", actor_id="op1", site_id="s1")
        plan_id = plan["plan_id"]
        self.festival.sign_off(request_id="s1-sign", actor_id="rv1",
                               plan_id=plan_id, check_item="fire_check")
        self.festival.sign_off(request_id="s2-sign", actor_id="rv2",
                               plan_id=plan_id, check_item="lantern_power")
        self.festival.update_resource(
            request_id="r-window-2", actor_id="op1", site_id="s1",
            resource_type="site_window", resource_key="s1",
            payload={"windows": [{"start": "2026-09-25T10:00:00Z", "end": "2026-09-25T15:00:00Z"}]})
        with self.assertRaises(ConflictError):
            self.festival.release_plan(request_id="rel", actor_id="op1", plan_id=plan_id)
        chain = self.festival.responsibility_chain(plan_id)
        changed_keys = {(item["resource_type"], item["resource_key"])
                        for item in chain["resource_changes"]}
        self.assertIn(("site_window", "s1"), changed_keys)

    def test_unit_revision_after_generation_blocks_release(self):
        self._resources()
        self._standard_units()
        plan = self.festival.generate_plan(request_id="p1", actor_id="op1", site_id="s1")
        plan_id = plan["plan_id"]
        self.festival.sign_off(request_id="s1-sign", actor_id="rv1",
                               plan_id=plan_id, check_item="fire_check")
        self.festival.sign_off(request_id="s2-sign", actor_id="rv2",
                               plan_id=plan_id, check_item="lantern_power")
        self._unit("u-revise", "poetry", "2026-09-25T11:00:00Z", "2026-09-25T12:15:00Z")
        with self.assertRaises(ConflictError):
            self.festival.release_plan(request_id="rel", actor_id="op1", plan_id=plan_id)

    # --------------------------------------------------------------
    # 生命周期
    # --------------------------------------------------------------

    def test_lifecycle_order_and_stable_repeats(self):
        self._released_plan()
        with self.assertRaises(ConflictError):
            self.festival.transition_unit(request_id="t-bad", actor_id="op1",
                                          unit_id="poetry", action="pause")
        started = self.festival.transition_unit(request_id="t-start", actor_id="op1",
                                                unit_id="poetry", action="start")
        self.assertEqual("in_progress", started["status"])
        paused = self.festival.transition_unit(request_id="t-pause", actor_id="op1",
                                               unit_id="poetry", action="pause")
        self.assertEqual("paused", paused["status"])
        paused_again = self.festival.transition_unit(request_id="t-pause-2", actor_id="op1",
                                                     unit_id="poetry", action="pause")
        self.assertTrue(paused_again["noop"])
        self.assertEqual("paused", paused_again["status"])
        resumed = self.festival.transition_unit(request_id="t-resume", actor_id="op1",
                                                unit_id="poetry", action="resume")
        self.assertEqual("in_progress", resumed["status"])
        finished = self.festival.transition_unit(request_id="t-finish", actor_id="op1",
                                                 unit_id="poetry", action="finish")
        self.assertEqual("finished", finished["status"])
        with self.assertRaises(ConflictError):
            self.festival.transition_unit(request_id="t-after-finish", actor_id="op1",
                                          unit_id="poetry", action="pause")

    def test_finish_releases_capacity(self):
        self._released_plan()
        self.festival.transition_unit(request_id="t-start", actor_id="op1",
                                      unit_id="lantern", action="start")
        self.festival.transition_unit(request_id="t-finish", actor_id="op1",
                                      unit_id="lantern", action="finish")
        capacity = self.festival.capacity_view("s1")
        lantern_entries = [entry for entry in capacity["entries"] if entry["unit_id"] == "lantern"]
        self.assertEqual([], lantern_entries)

    def test_cancel_only_before_start_and_releases_capacity(self):
        self._released_plan()
        cancelled = self.festival.transition_unit(request_id="t-cancel", actor_id="op1",
                                                  unit_id="folk", action="cancel")
        self.assertEqual("cancelled", cancelled["status"])
        self.festival.transition_unit(request_id="t-start", actor_id="op1",
                                      unit_id="lantern", action="start")
        with self.assertRaises(ConflictError):
            self.festival.transition_unit(request_id="t-cancel-2", actor_id="op1",
                                          unit_id="lantern", action="cancel")
        capacity = self.festival.capacity_view("s1")
        self.assertEqual([], [e for e in capacity["entries"] if e["unit_id"] == "folk"])

    def test_cancelled_dependency_blocks_clearance(self):
        self._released_plan()
        self.festival.transition_unit(request_id="t-cancel", actor_id="op1",
                                      unit_id="poetry", action="cancel")
        self.festival.transition_unit(request_id="t-cancel-2", actor_id="op1",
                                      unit_id="folk", action="cancel")
        self._unit("u-new", "story", "2026-09-25T13:30:00Z", "2026-09-25T14:30:00Z",
                   deps=("poetry",))
        clearance = self.festival.clearance("s1")
        story = next(item for item in clearance["items"] if item["unit_id"] == "story")
        self.assertIn("dependency_cancelled", {b["code"] for b in story["blockers"]})

    # --------------------------------------------------------------
    # 影响分析与责任链
    # --------------------------------------------------------------

    def test_impact_suggests_relocation_only_for_not_started_units(self):
        self._released_plan()
        self.festival.transition_unit(request_id="t-start", actor_id="op1",
                                      unit_id="lantern", action="start")
        impact = self.festival.report_impact(request_id="i1", actor_id="rv1",
                                             site_id="s1", kind="rain_alert",
                                             detail={"level": "orange"})
        by_unit = {item["unit_id"]: item for item in impact["assessments"]}
        self.assertEqual("relocate_suggested", by_unit["poetry"]["disposition"])
        self.assertEqual("s2", by_unit["poetry"]["suggestion"]["fallback_site_id"])
        self.assertEqual("manual_handling", by_unit["lantern"]["disposition"])
        self.assertFalse(by_unit["lantern"]["suggestion"]["relocatable"])
        self.assertEqual("relocate_suggested", by_unit["folk"]["disposition"])
        self.assertEqual("s2", by_unit["folk"]["suggestion"]["fallback_site_id"])

    def test_impact_without_fallback_marks_unavailable(self):
        self._resources()
        self._standard_units()
        self.festival.generate_plan(request_id="p1", actor_id="op1", site_id="s1")
        connection = self.database.connection
        connection.execute("DELETE FROM festival_resources WHERE resource_type='rain_fallback'")
        impact = self.festival.report_impact(request_id="i1", actor_id="rv1",
                                             site_id="s1", kind="rain_alert")
        folk = next(item for item in impact["assessments"] if item["unit_id"] == "folk")
        self.assertEqual("relocate_suggested", folk["disposition"])
        self.assertFalse(folk["suggestion"]["relocatable"])

    def test_responsibility_chain_tracks_adjustments(self):
        plan_id = self._released_plan()
        self.festival.update_resource(
            request_id="r-fire-2", actor_id="a1", site_id="s1",
            resource_type="material_check", resource_key="fire_check",
            payload={"responsible_actor": "rv1", "description": "消防检查（复核）"})
        chain = self.festival.responsibility_chain(plan_id)
        self.assertEqual("op1", chain["created_by"])
        self.assertEqual("op1", chain["released_by"])
        signers = {item["check_item"]: item["signed_by"] for item in chain["sign_offs"]}
        self.assertEqual({"fire_check": "rv1", "lantern_power": "rv2"}, signers)
        before = {(item["resource_type"], item["resource_key"]): item
                  for item in chain["resources_before"]}
        after = {(item["resource_type"], item["resource_key"]): item
                 for item in chain["resources_after"]}
        key = ("material_check", "fire_check")
        self.assertEqual(1, before[key]["version"])
        self.assertEqual(2, after[key]["version"])
        self.assertEqual("a1", after[key]["maintained_by"])
        self.assertEqual([key], [(item["resource_type"], item["resource_key"])
                                 for item in chain["resource_changes"]])

    def test_audit_chain_covers_festival_events(self):
        self._released_plan()
        self.festival.report_impact(request_id="i1", actor_id="rv1",
                                    site_id="s1", kind="rain_alert")
        valid, count = self.domain.verify_audit()
        self.assertTrue(valid)
        actions = {event["action"] for event in self.domain.audit_events()}
        self.assertIn("festival.plan.generated", actions)
        self.assertIn("festival.plan.released", actions)
        self.assertIn("festival.impact.reported", actions)
        self.assertGreater(count, 0)


if __name__ == "__main__":
    unittest.main()
