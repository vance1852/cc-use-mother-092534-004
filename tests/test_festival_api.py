import unittest
from datetime import datetime, timezone

from festival_foundation.api import route
from festival_foundation.clock import FixedClock
from festival_foundation.festival import FestivalService
from festival_foundation.service import DomainService
from festival_foundation.storage import Database


class FestivalApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DomainService(
            self.database, FixedClock(datetime(2026, 9, 25, tzinfo=timezone.utc)))
        self.festival = FestivalService(self.service)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="社区文化中心")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="planner", actor_id="a1", new_actor_id="op1",
                                    display_name="策划人", role="operator", organization_id="o1")
        self.service.register_actor(request_id="safety", actor_id="a1", new_actor_id="rv1",
                                    display_name="安全员", role="reviewer", organization_id="o1")
        self.service.register_site(request_id="site", actor_id="op1", site_id="s1",
                                   organization_id="o1", name="中心广场",
                                   timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def _post(self, path, body, actor="op1"):
        return route(self.service, "POST", path, body, {"X-Actor-Id": actor},
                     festival=self.festival)

    def _get(self, path):
        return route(self.service, "GET", path, None, festival=self.festival)

    def _prepare_unit(self):
        self._post("/festival/resources", {
            "request_id": "r-window", "site_id": "s1",
            "resource_type": "site_window", "resource_key": "s1",
            "payload": {"windows": [{"start": "2026-09-25T10:00:00Z",
                                     "end": "2026-09-25T16:00:00Z"}]}})
        self._post("/festival/resources", {
            "request_id": "r-capacity", "site_id": "s1",
            "resource_type": "capacity_limit", "resource_key": "s1",
            "payload": {"max_headcount": 500}})
        self._post("/festival/resources", {
            "request_id": "r-qual", "site_id": "s1",
            "resource_type": "qualification", "resource_key": "op1",
            "payload": {"qualifications": ["crowd_control"]}})
        self._post("/festival/resources", {
            "request_id": "r-fire", "site_id": "s1",
            "resource_type": "material_check", "resource_key": "fire_check",
            "payload": {"responsible_actor": "rv1"}})
        status, payload = self._post("/festival/units", {
            "request_id": "u1", "unit_id": "poetry", "site_id": "s1", "title": "中秋诗会",
            "kind": "poetry", "planned_start": "2026-09-25T11:00:00Z",
            "planned_end": "2026-09-25T12:00:00Z", "expected_headcount": 100,
            "required_qualifications": ["crowd_control"], "required_checks": ["fire_check"]})
        self.assertEqual(201, status)
        self.assertEqual(1, payload["version"])

    def test_unit_submission_and_replay_over_http(self):
        self._prepare_unit()
        status, payload = self._post("/festival/units", {
            "request_id": "u1", "unit_id": "poetry", "site_id": "s1", "title": "中秋诗会",
            "kind": "poetry", "planned_start": "2026-09-25T11:00:00Z",
            "planned_end": "2026-09-25T12:00:00Z", "expected_headcount": 100,
            "required_qualifications": ["crowd_control"], "required_checks": ["fire_check"]})
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])
        status, payload = self._get("/festival/units?site_id=s1")
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))
        self.assertEqual("poetry", payload["items"][0]["unit_id"])

    def test_full_release_flow_over_http(self):
        self._prepare_unit()
        status, plan = self._post("/festival/plans", {"request_id": "p1", "site_id": "s1"})
        self.assertEqual(201, status)
        plan_id = plan["plan_id"]
        status, payload = self._post("/festival/plans/release",
                                     {"request_id": "rel-early", "plan_id": plan_id})
        self.assertEqual(409, status)
        self.assertIn("签署", payload["message"])
        status, payload = self._post("/festival/plans/sign-off",
                                     {"request_id": "s1", "plan_id": plan_id,
                                      "check_item": "fire_check"}, actor="op1")
        self.assertEqual(403, status)
        status, payload = self._post("/festival/plans/sign-off",
                                     {"request_id": "s2", "plan_id": plan_id,
                                      "check_item": "fire_check"}, actor="rv1")
        self.assertEqual(201, status)
        status, payload = self._post("/festival/plans/release",
                                     {"request_id": "rel", "plan_id": plan_id})
        self.assertEqual(201, status)
        self.assertEqual("released", payload["status"])
        status, payload = self._get(f"/festival/responsibility?plan_id={plan_id}")
        self.assertEqual(200, status)
        self.assertEqual("op1", payload["released_by"])
        status, payload = self._post("/festival/units/transition",
                                     {"request_id": "t1", "unit_id": "poetry", "action": "start"})
        self.assertEqual(201, status)
        self.assertEqual("in_progress", payload["status"])
        status, payload = self._post("/festival/impacts",
                                     {"request_id": "i1", "site_id": "s1", "kind": "rain_alert"},
                                     actor="rv1")
        self.assertEqual(201, status)
        assessment = payload["assessments"][0]
        self.assertEqual("manual_handling", assessment["disposition"])
        status, payload = self._get(f"/festival/impact?impact_id={payload['impact_id']}")
        self.assertEqual(200, status)
        self.assertEqual("rain_alert", payload["kind"])

    def test_clearance_and_capacity_endpoints(self):
        self._prepare_unit()
        status, payload = self._get("/festival/clearance?site_id=s1")
        self.assertEqual(200, status)
        self.assertTrue(payload["items"][0]["clearable"])
        status, payload = self._get("/festival/capacity?site_id=s1")
        self.assertEqual(200, status)
        self.assertEqual(500, payload["max_headcount"])

    def test_unknown_festival_route_returns_404(self):
        status, payload = self._get("/festival/unknown")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_festival_route_requires_service(self):
        status, payload = route(self.service, "GET", "/festival/units?site_id=s1", None)
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
