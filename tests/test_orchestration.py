"""活动承载与安全放行中枢的领域规则测试。"""

import unittest
from datetime import datetime, timezone

from festival_foundation.clock import FixedClock
from festival_foundation.errors import (
    ConflictError, NotFoundError, PermissionDenied, ValidationError,
)
from festival_foundation.orchestration import OrchestrationService
from festival_foundation.storage import Database

START = "2026-09-27T18:00:00+08:00"
END = "2026-09-27T20:30:00+08:00"


class HubCase(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = OrchestrationService(
            self.database, FixedClock(datetime(2026, 9, 25, tzinfo=timezone.utc)))
        s = self.service
        s.register_organization(request_id="org-init", actor_id="bootstrap",
                                organization_id="o1", name="社区")
        s.register_actor(request_id="actor-admin", actor_id="bootstrap", new_actor_id="a1",
                         display_name="管理员", role="admin", organization_id="o1")
        s.register_actor(request_id="actor-planner", actor_id="a1", new_actor_id="pl1",
                         display_name="策划人", role="operator", organization_id="o1")
        s.register_actor(request_id="actor-fire", actor_id="a1", new_actor_id="sf1",
                         display_name="消防安全员", role="reviewer", organization_id="o1")
        s.register_actor(request_id="actor-mat", actor_id="a1", new_actor_id="sm1",
                         display_name="物资安全员", role="reviewer", organization_id="o1")
        s.register_actor(request_id="actor-vol", actor_id="a1", new_actor_id="v1",
                         display_name="志愿者", role="operator", organization_id="o1")
        s.register_actor(request_id="actor-vol2", actor_id="a1", new_actor_id="v2",
                         display_name="无资格志愿者", role="operator", organization_id="o1")
        s.register_site(request_id="site-plaza", actor_id="pl1", site_id="plaza",
                        organization_id="o1", name="露天广场", timezone_name="Asia/Shanghai")
        s.register_site(request_id="site-hall", actor_id="pl1", site_id="hall",
                        organization_id="o1", name="室内礼堂", timezone_name="Asia/Shanghai")
        s.configure_site_resource(request_id="res-plaza", actor_id="pl1", site_id="plaza",
                                  max_headcount=200, indoor=False)
        s.configure_site_resource(request_id="res-hall", actor_id="pl1", site_id="hall",
                                  max_headcount=300, indoor=True)
        s.upsert_site_window(request_id="win-plaza", actor_id="pl1", window_id="wplaza",
                             site_id="plaza",
                             window_start="2026-09-27T10:00:00+08:00",
                             window_end="2026-09-27T23:00:00+08:00")
        s.upsert_site_window(request_id="win-hall", actor_id="pl1", window_id="whall",
                             site_id="hall",
                             window_start="2026-09-27T10:00:00+08:00",
                             window_end="2026-09-27T23:00:00+08:00")
        s.grant_certification(request_id="cert-vol", actor_id="a1", target_actor_id="v1",
                              cert_code="volunteer_basic")
        s.assign_safety_check(request_id="chk-fire-plaza", actor_id="pl1", check_id="fire-plaza",
                              scope_type="site", scope_key="plaza", check_kind="fire",
                              title="广场消防检查", owner_actor_id="sf1")
        s.assign_safety_check(request_id="chk-fire-hall", actor_id="pl1", check_id="fire-hall",
                              scope_type="site", scope_key="hall", check_kind="fire",
                              title="礼堂消防检查", owner_actor_id="sf1")
        s.assign_safety_check(request_id="chk-mat-lantern", actor_id="pl1", check_id="mat-lantern",
                              scope_type="unit", scope_key="lantern", check_kind="material",
                              title="花灯用电检查", owner_actor_id="sm1")
        self.seq = 0

    def tearDown(self):
        self.database.close()

    def rid(self, prefix: str) -> str:
        self.seq += 1
        return f"{prefix}-{self.seq:03d}"

    def unit(self, key, *, site="plaza", start="2026-09-27T18:00:00+08:00",
             end="2026-09-27T19:00:00+08:00", headcount=80, posts=None, depends=None,
             backup="hall", name=None):
        return {"key": key, "name": name or key, "site_id": site, "start_time": start,
                "end_time": end, "expected_headcount": headcount,
                "posts": posts if posts is not None else
                [{"post": "引导岗", "required_cert": "volunteer_basic", "actor_id": "v1"}],
                "depends_on": depends or [], "rain_backup_site_id": backup}

    def submit_default_plan(self, plan_id="plan1", units=None):
        units = units or [
            self.unit("poetry"),
            self.unit("lantern", start="2026-09-27T19:00:00+08:00",
                      end="2026-09-27T20:30:00+08:00", headcount=100, depends=["poetry"]),
        ]
        return self.service.submit_plan(request_id=self.rid("submit"), actor_id="pl1",
                                        plan_id=plan_id, name="中秋同晚活动", units=units)

    def publish_default_plan(self, plan_id="plan1"):
        gate = self.service.get_plan_gate(plan_id)
        for check in gate["required_checks"]:
            self.service.sign_check(request_id=self.rid("sign"),
                                    actor_id=check["owner_actor_id"], plan_id=plan_id,
                                    check_id=check["check_id"])
        return self.service.publish_plan(request_id=self.rid("publish"), actor_id="pl1",
                                         plan_id=plan_id)


class PlanSubmissionTest(HubCase):
    def test_changed_unit_gets_new_version_others_keep_theirs(self):
        self.submit_default_plan()
        self.publish_default_plan()
        # 修改花灯时间，诗会内容不变。
        result = self.service.submit_plan(
            request_id=self.rid("submit"), actor_id="pl1", plan_id="plan1", name="中秋同晚活动",
            units=[self.unit("poetry"),
                   self.unit("lantern", start="2026-09-27T19:30:00+08:00",
                             end="2026-09-27T21:00:00+08:00", headcount=100, depends=["poetry"])])
        versions = {u["key"]: u["version"] for u in result["units"]}
        self.assertEqual(1, versions["poetry"])
        self.assertEqual(2, versions["lantern"])

    def test_identical_resubmit_is_stable(self):
        units = [self.unit("poetry"),
                 self.unit("lantern", start="2026-09-27T19:00:00+08:00",
                           end="2026-09-27T20:30:00+08:00", headcount=100, depends=["poetry"])]
        first = self.service.submit_plan(request_id=self.rid("submit"), actor_id="pl1",
                                         plan_id="plan1", name="中秋", units=units)
        again = self.service.submit_plan(request_id=self.rid("submit"), actor_id="pl1",
                                         plan_id="plan1", name="中秋", units=list(reversed(units)))
        self.assertTrue(again["unchanged"])
        self.assertEqual(first["revision_no"], again["revision_no"])

    def test_dependency_must_exist_and_be_acyclic(self):
        with self.assertRaises(ValidationError):
            self.submit_default_plan(units=[self.unit("ua", depends=["ghost"])])
        with self.assertRaises(ValidationError):
            self.submit_default_plan(units=[
                self.unit("ua", depends=["b"]), self.unit("ub", depends=["ua"])])

    def test_end_must_be_after_start(self):
        with self.assertRaises(ValidationError):
            self.submit_default_plan(units=[
                self.unit("ua", start="2026-09-27T20:00:00+08:00", end="2026-09-27T19:00:00+08:00")])


class GateEvaluationTest(HubCase):
    def test_gate_lists_each_blocking_condition(self):
        self.submit_default_plan(units=[self.unit(
            "poetry", headcount=500,
            posts=[{"post": "引导岗", "required_cert": "volunteer_basic", "actor_id": "v2"}],
            start="2026-09-28T01:00:00+08:00", end="2026-09-28T02:00:00+08:00", backup=None)])
        gate = self.service.get_plan_gate("plan1")
        self.assertFalse(gate["releasable"])
        codes = {b["code"] for b in gate["plan_blockers"]}
        self.assertIn("signature_missing", codes)
        unit = gate["units"]["poetry"]
        unit_codes = {b["code"] for b in unit["blockers"]}
        self.assertIn("site_headcount_exceeded", unit_codes)
        self.assertIn("outside_open_window", unit_codes)
        self.assertIn("missing_certification", unit_codes)

    def test_capacity_overlap_blocks_cross_plan(self):
        self.submit_default_plan("plan1", [self.unit("ua", headcount=150)])
        self.publish_default_plan("plan1")
        self.submit_default_plan("plan2", [
            self.unit("ub", start="2026-09-27T18:30:00+08:00",
                      end="2026-09-27T19:30:00+08:00", headcount=100, backup=None)])
        gate = self.service.get_plan_gate("plan2")
        codes = {b["code"] for b in gate["plan_blockers"]}
        self.assertIn("site_capacity_exceeded", codes)

    def test_non_overlapping_units_share_site_fine(self):
        self.submit_default_plan("plan1", [
            self.unit("ua", start="2026-09-27T18:00:00+08:00", end="2026-09-27T19:00:00+08:00",
                      headcount=150),
            self.unit("ub", start="2026-09-27T19:00:00+08:00", end="2026-09-27T20:00:00+08:00",
                      headcount=150, depends=["ua"], backup=None)])
        gate = self.service.get_plan_gate("plan1")
        self.assertNotIn("site_capacity_exceeded",
                         {b["code"] for b in gate["plan_blockers"]})

    def test_closed_site_blocks(self):
        self.service.report_facility_change(
            request_id=self.rid("fac"), actor_id="pl1", incident_id="inc-close",
            site_id="plaza", facility_status="closed")
        self.submit_default_plan()
        gate = self.service.get_plan_gate("plan1")
        self.assertIn("site_closed", {b["code"] for u in gate["units"].values()
                                      for b in u["blockers"]})

    def test_missing_fire_assignment_is_itself_a_blocker(self):
        # 一个没有任何消防检查项的新场地。
        self.service.register_site(request_id=self.rid("site"), actor_id="pl1", site_id="roof",
                                   organization_id="o1", name="天台", timezone_name="Asia/Shanghai")
        self.service.configure_site_resource(request_id=self.rid("res"), actor_id="pl1",
                                             site_id="roof", max_headcount=100, indoor=False)
        self.service.upsert_site_window(request_id=self.rid("win"), actor_id="pl1",
                                        window_id="wroof", site_id="roof",
                                        window_start="2026-09-27T10:00:00+08:00",
                                        window_end="2026-09-27T23:00:00+08:00")
        self.submit_default_plan(units=[self.unit("ua", site="roof", backup=None)])
        gate = self.service.get_plan_gate("plan1")
        self.assertIn("missing_check_assignment", {b["code"] for b in gate["plan_blockers"]})


class SignoffAndPublishTest(HubCase):
    def test_only_owner_can_sign(self):
        self.submit_default_plan()
        with self.assertRaises(PermissionDenied):
            self.service.sign_check(request_id=self.rid("sign"), actor_id="sf1",
                                    plan_id="plan1", check_id="mat-lantern")
        with self.assertRaises(PermissionDenied):
            self.service.sign_check(request_id=self.rid("sign"), actor_id="pl1",
                                    plan_id="plan1", check_id="fire-plaza")

    def test_publish_requires_all_required_signatures(self):
        self.submit_default_plan()
        self.service.sign_check(request_id=self.rid("sign"), actor_id="sf1",
                                plan_id="plan1", check_id="fire-plaza")
        with self.assertRaises(ConflictError):
            self.service.publish_plan(request_id=self.rid("publish"), actor_id="pl1",
                                      plan_id="plan1")
        # 发布失败不留容量承诺。
        self.assertEqual([], self.service.capacity_ledger("plaza")["commits"])
        self.service.sign_check(request_id=self.rid("sign"), actor_id="sm1",
                                plan_id="plan1", check_id="mat-lantern")
        published = self.service.publish_plan(request_id=self.rid("publish"), actor_id="pl1",
                                              plan_id="plan1")
        self.assertEqual("published", published["status"])
        self.assertEqual(2, len(self.service.capacity_ledger("plaza")["commits"]))

    def test_resource_change_invalidates_signature(self):
        self.submit_default_plan()
        self.publish_default_plan()
        # 策划人改花灯人数，产生新草稿，物资签署作用域变化。
        self.service.submit_plan(
            request_id=self.rid("submit"), actor_id="pl1", plan_id="plan1", name="中秋同晚活动",
            units=[self.unit("poetry"),
                   self.unit("lantern", headcount=120, start="2026-09-27T19:00:00+08:00",
                             end="2026-09-27T20:30:00+08:00", depends=["poetry"])])
        gate = self.service.get_plan_gate("plan1")
        statuses = {c["check_id"]: c["status"] for c in gate["required_checks"]}
        self.assertEqual("stale", statuses["mat-lantern"])
        self.assertEqual("carried", statuses["fire-plaza"])

    def test_site_resource_change_invalidates_site_signature(self):
        self.submit_default_plan(units=[self.unit("ua", backup=None)])
        self.publish_default_plan()
        self.service.report_facility_change(
            request_id=self.rid("fac"), actor_id="pl1", incident_id="inc-fc",
            site_id="plaza", facility_status="available", max_headcount=250)
        self.service.submit_plan(
            request_id=self.rid("submit"), actor_id="pl1", plan_id="plan1",
            name="中秋同晚活动", units=[self.unit("ua", headcount=90, backup=None)])
        gate = self.service.get_plan_gate("plan1")
        self.assertEqual("stale", next(c["status"] for c in gate["required_checks"]
                                       if c["check_id"] == "fire-plaza"))

    def test_publish_is_one_shot_and_revision_chain(self):
        self.submit_default_plan()
        first = self.publish_default_plan()
        self.assertEqual(1, first["revision_no"])
        revisions = self.database.connection.execute(
            "SELECT revision_no,status FROM plan_revisions ORDER BY revision_no").fetchall()
        self.assertEqual([(1, "published")], [tuple(r) for r in revisions])
        # 新修订发布后旧修订被 superseded。
        self.service.submit_plan(
            request_id=self.rid("submit"), actor_id="pl1", plan_id="plan1", name="中秋同晚活动",
            units=[self.unit("poetry", headcount=90),
                   self.unit("lantern", start="2026-09-27T19:00:00+08:00",
                             end="2026-09-27T20:30:00+08:00", headcount=100, depends=["poetry"])])
        gate = self.service.get_plan_gate("plan1")
        for check in gate["required_checks"]:
            if check["status"] in ("missing", "stale"):
                self.service.sign_check(request_id=self.rid("sign"),
                                        actor_id=check["owner_actor_id"], plan_id="plan1",
                                        check_id=check["check_id"])
        second = self.service.publish_plan(request_id=self.rid("publish"), actor_id="pl1",
                                           plan_id="plan1")
        self.assertEqual(2, second["revision_no"])
        statuses = {r["revision_no"]: r["status"] for r in self.database.connection.execute(
            "SELECT revision_no,status FROM plan_revisions")}
        self.assertEqual("superseded", statuses[1])
        self.assertEqual("published", statuses[2])

    def test_resource_change_between_sign_and_publish_blocks_release(self):
        self.submit_default_plan(units=[self.unit("ua", backup=None)])
        gate = self.service.get_plan_gate("plan1")
        for check in gate["required_checks"]:
            self.service.sign_check(request_id=self.rid("sign"),
                                    actor_id=check["owner_actor_id"], plan_id="plan1",
                                    check_id=check["check_id"])
        self.assertTrue(self.service.get_plan_gate("plan1")["releasable"])
        # 签署完成后场地资源版本变化（其他人员维护），旧签署变为 stale。
        self.service.configure_site_resource(request_id=self.rid("res"), actor_id="pl1",
                                             site_id="plaza", max_headcount=260, indoor=False)
        gate = self.service.get_plan_gate("plan1")
        self.assertFalse(gate["releasable"])
        self.assertIn("signature_stale", {b["code"] for b in gate["plan_blockers"]})
        with self.assertRaises(ConflictError):
            self.service.publish_plan(request_id=self.rid("publish"), actor_id="pl1",
                                      plan_id="plan1")
        # 发布失败原子回滚：修订仍为草稿，容量承诺未生成，没有发布事件。
        row = self.database.connection.execute(
            "SELECT status FROM plan_revisions WHERE plan_id='plan1'").fetchone()
        self.assertEqual("drafting", row["status"])
        self.assertEqual([], self.service.capacity_ledger("plaza")["commits"])
        publish_events = self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM audit_events WHERE action='plan.published'").fetchone()["c"]
        self.assertEqual(0, publish_events)


class IncidentImpactTest(HubCase):
    def test_rain_splits_live_and_scheduled_units(self):
        self.submit_default_plan()
        self.publish_default_plan()
        self.service.start_unit(request_id=self.rid("start"), actor_id="pl1",
                                plan_id="plan1", unit_key="poetry")
        rain = self.service.declare_rain_alert(
            request_id=self.rid("rain"), actor_id="pl1", incident_id="inc-rain")
        self.assertEqual(["poetry"], [u["unit_key"] for u in rain["manual_units"]])
        self.assertEqual([("lantern", "hall")],
                         [(s["unit_key"], s["to_site_id"]) for s in rain["suggestions"]])
        impact = self.service.analyze_impact()
        self.assertEqual(["poetry"], [u["unit_key"] for u in impact["manual_units"]])
        lantern = next(r for r in impact["relocations"] if r["unit_key"] == "lantern")
        self.assertTrue(lantern["preview"]["feasible"])
        self.assertIn("signoff", {c["stage"] for c in lantern["responsibility_chain_before"]})

    def test_indoor_unit_not_affected_by_rain(self):
        self.submit_default_plan(units=[self.unit("ua", site="hall", backup=None)])
        self.publish_default_plan()
        rain = self.service.declare_rain_alert(
            request_id=self.rid("rain"), actor_id="pl1", incident_id="inc-rain")
        self.assertEqual([], rain["manual_units"])
        self.assertEqual([], rain["suggestions"])

    def test_live_unit_cannot_be_silently_relocated(self):
        self.submit_default_plan()
        self.publish_default_plan()
        self.service.start_unit(request_id=self.rid("start"), actor_id="pl1",
                                plan_id="plan1", unit_key="poetry")
        self.service.declare_rain_alert(request_id=self.rid("rain"), actor_id="pl1",
                                        incident_id="inc-rain")
        with self.assertRaises(ConflictError):
            self.service.accept_relocation(request_id=self.rid("acc"), actor_id="pl1",
                                           plan_id="plan1", unit_key="poetry")

    def test_manual_resume_and_end(self):
        self.submit_default_plan(units=[self.unit("ua", backup=None)])
        self.publish_default_plan()
        self.service.start_unit(request_id=self.rid("start"), actor_id="pl1",
                                plan_id="plan1", unit_key="ua")
        # 设施事件把进行中单元送入人工处置。
        self.service.report_facility_change(
            request_id=self.rid("fac"), actor_id="pl1", incident_id="inc-fac",
            site_id="plaza", facility_status="restricted")
        self.assertEqual("manual_handling", self.service.get_plan_gate("plan1")["units"]["ua"]["state"])
        resolved = self.service.resolve_manual_unit(
            request_id=self.rid("res"), actor_id="sf1", plan_id="plan1",
            unit_key="ua", decision="end")
        self.assertEqual("ended", resolved["state"])

    def test_relocation_requires_new_site_signoff_then_republishes(self):
        self.submit_default_plan()
        self.publish_default_plan()
        self.service.declare_rain_alert(request_id=self.rid("rain"), actor_id="pl1",
                                        incident_id="inc-rain")
        accepted = self.service.accept_relocation(
            request_id=self.rid("acc"), actor_id="pl1", plan_id="plan1", unit_key="lantern")
        self.assertEqual(2, accepted["revision_no"])
        gate = self.service.get_plan_gate("plan1")
        self.assertFalse(gate["releasable"])
        # 广场消防沿用，礼堂消防与花灯物资需要重新签署。
        statuses = {c["check_id"]: c["status"] for c in gate["required_checks"]}
        self.assertEqual("carried", statuses["fire-plaza"])
        self.assertEqual("missing", statuses["fire-hall"])
        self.assertIn(statuses["mat-lantern"], ("missing", "stale"))
        with self.assertRaises(ConflictError):
            self.service.publish_plan(request_id=self.rid("publish"), actor_id="pl1",
                                      plan_id="plan1")
        self.service.sign_check(request_id=self.rid("sign"), actor_id="sf1",
                                plan_id="plan1", check_id="fire-hall")
        self.service.sign_check(request_id=self.rid("sign"), actor_id="sm1",
                                plan_id="plan1", check_id="mat-lantern")
        republished = self.service.publish_plan(request_id=self.rid("publish"), actor_id="pl1",
                                                plan_id="plan1")
        self.assertEqual("hall", republished["gate"]["units"]["lantern"]["site_id"])
        plaza_commits = self.service.capacity_ledger("plaza")["commits"]
        self.assertEqual(["poetry"], [c["unit_key"] for c in plaza_commits])
        hall_commits = self.service.capacity_ledger("hall")["commits"]
        self.assertEqual(["lantern"], [c["unit_key"] for c in hall_commits])


class LifecycleTest(HubCase):
    def _live_plan(self):
        self.submit_default_plan(units=[self.unit("ua", backup=None)])
        self.publish_default_plan()

    def test_happy_path_and_idempotent_repeats(self):
        self._live_plan()
        rid = self.rid("start")
        first = self.service.start_unit(request_id=rid, actor_id="pl1", plan_id="plan1",
                                        unit_key="ua")
        replay = self.service.start_unit(request_id=rid, actor_id="pl1", plan_id="plan1",
                                         unit_key="ua")
        self.assertEqual("in_progress", first["state"])
        self.assertTrue(replay["reapplied"])

    def test_illegal_order_is_rejected_with_hint(self):
        self._live_plan()
        with self.assertRaises(ConflictError):  # 未开始不能暂停
            self.service.pause_unit(request_id=self.rid("pause"), actor_id="pl1",
                                    plan_id="plan1", unit_key="ua")
        self.service.start_unit(request_id=self.rid("start"), actor_id="pl1",
                                plan_id="plan1", unit_key="ua")
        with self.assertRaises(ConflictError):  # 已开始不能取消
            self.service.cancel_unit(request_id=self.rid("cancel"), actor_id="pl1",
                                     plan_id="plan1", unit_key="ua")
        self.service.pause_unit(request_id=self.rid("pause"), actor_id="pl1",
                                plan_id="plan1", unit_key="ua")
        with self.assertRaises(ConflictError):  # 暂停中不能直接结束？可以结束；暂停中不能 start
            self.service.start_unit(request_id=self.rid("start2"), actor_id="pl1",
                                    plan_id="plan1", unit_key="ua")
        self.service.resume_unit(request_id=self.rid("resume"), actor_id="pl1",
                                 plan_id="plan1", unit_key="ua")
        self.service.end_unit(request_id=self.rid("end"), actor_id="pl1",
                              plan_id="plan1", unit_key="ua")
        with self.assertRaises(ConflictError):  # 已结束不能再取消
            self.service.cancel_unit(request_id=self.rid("cancel2"), actor_id="pl1",
                                     plan_id="plan1", unit_key="ua")

    def test_cancel_releases_capacity(self):
        self._live_plan()
        self.service.cancel_unit(request_id=self.rid("cancel"), actor_id="pl1",
                                 plan_id="plan1", unit_key="ua")
        ledger = self.service.capacity_ledger("plaza")
        self.assertEqual("cancelled", ledger["commits"][0]["state"])


class PermissionBoundaryTest(HubCase):
    def test_reviewer_cannot_publish_or_plan(self):
        with self.assertRaises(PermissionDenied):
            self.submit_default_plan() if False else self.service.submit_plan(
                request_id=self.rid("submit"), actor_id="sf1", plan_id="plan1", name="x",
                units=[self.unit("ua")])
        self.submit_default_plan()
        with self.assertRaises(PermissionDenied):
            self.service.publish_plan(request_id=self.rid("publish"), actor_id="sf1",
                                      plan_id="plan1")

    def test_org_isolation(self):
        self.service.register_organization(request_id=self.rid("org"), actor_id="a1",
                                           organization_id="o2", name="其他社区")
        self.service.register_actor(request_id=self.rid("actor"), actor_id="a1",
                                    new_actor_id="pl2", display_name="外社区策划",
                                    role="operator", organization_id="o2")
        with self.assertRaises(PermissionDenied):
            self.service.configure_site_resource(request_id=self.rid("res"), actor_id="pl2",
                                                 site_id="plaza", max_headcount=10, indoor=False)
        with self.assertRaises(PermissionDenied):
            self.service.submit_plan(
                request_id=self.rid("submit"), actor_id="pl2", plan_id="planx", name="x",
                units=[self.unit("ua")])


class BoardAndChainTest(HubCase):
    def test_board_marks_released_and_blockers(self):
        self.submit_default_plan()
        gate = self.service.get_plan_gate("plan1")
        self.assertFalse(gate["releasable"])
        self.publish_default_plan()
        board = {i["unit_key"]: i for i in self.service.release_board()["items"]}
        self.assertTrue(board["poetry"]["released"])
        self.assertEqual([], board["poetry"]["blockers"])

    def test_impact_chain_shows_before_and_after(self):
        self.submit_default_plan()
        self.publish_default_plan()
        self.service.declare_rain_alert(request_id=self.rid("rain"), actor_id="pl1",
                                        incident_id="inc-rain")
        impact = self.service.analyze_impact("plan1")
        lantern = next(r for r in impact["relocations"] if r["unit_key"] == "lantern")
        before_stages = [c["stage"] for c in lantern["responsibility_chain_before"]]
        after_stages = [(c["stage"], c.get("status")) for c in lantern["responsibility_chain_after"]]
        self.assertEqual(["planning", "signoff", "signoff", "publish"], before_stages)
        # 调整后：广场消防沿用、花灯物资失效、礼堂消防待签。
        after_map = {c.get("scope"): c.get("status") for c in lantern["responsibility_chain_after"]
                     if c["stage"] == "signoff"}
        self.assertEqual("carried", after_map["site:plaza"])
        self.assertEqual("stale", after_map["unit:lantern"])
        self.assertEqual("missing", after_map["site:hall"])


if __name__ == "__main__":
    unittest.main()
