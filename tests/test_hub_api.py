"""活动承载与安全放行中枢的 HTTP 路由测试。"""

import unittest

from festival_foundation.api import route
from festival_foundation.orchestration import OrchestrationService
from festival_foundation.storage import Database


def call(service, method, path, body=None, actor=""):
    return route(service, method, path, body or {}, {"X-Actor-Id": actor})


class HubApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = OrchestrationService(self.database)
        c = lambda *a, **k: call(self.service, *a, **k)
        c("POST", "/organizations", {"request_id": "org-init", "organization_id": "o1",
                                     "name": "社区"}, "bootstrap")
        c("POST", "/actors", {"request_id": "actor-admin", "new_actor_id": "a1",
                              "display_name": "管理员", "role": "admin",
                              "organization_id": "o1"}, "bootstrap")
        c("POST", "/actors", {"request_id": "actor-planner", "new_actor_id": "pl1",
                              "display_name": "策划", "role": "operator",
                              "organization_id": "o1"}, "a1")
        c("POST", "/actors", {"request_id": "actor-fire", "new_actor_id": "sf1",
                              "display_name": "消防安全员", "role": "reviewer",
                              "organization_id": "o1"}, "a1")
        c("POST", "/sites", {"request_id": "site-plaza", "site_id": "plaza",
                             "organization_id": "o1", "name": "广场",
                             "timezone_name": "Asia/Shanghai"}, "pl1")
        c("POST", "/site-resources", {"request_id": "res-plaza", "site_id": "plaza",
                                      "max_headcount": 200, "indoor": False}, "pl1")
        c("POST", "/site-windows", {"request_id": "win-plaza", "window_id": "wp",
                                    "site_id": "plaza",
                                    "window_start": "2026-09-27T10:00:00+08:00",
                                    "window_end": "2026-09-27T23:00:00+08:00"}, "pl1")
        c("POST", "/certifications", {"request_id": "cert-v", "target_actor_id": "pl1",
                                      "cert_code": "volunteer_basic"}, "a1")
        c("POST", "/safety-checks", {"request_id": "chk-fire", "check_id": "fire-plaza",
                                     "scope_type": "site", "scope_key": "plaza",
                                     "check_kind": "fire", "title": "消防",
                                     "owner_actor_id": "sf1"}, "pl1")

    def tearDown(self):
        self.database.close()

    def test_gate_blockers_then_sign_then_publish_over_http(self):
        unit = {"key": "poetry", "name": "诗会", "site_id": "plaza",
                "start_time": "2026-09-27T18:00:00+08:00",
                "end_time": "2026-09-27T19:00:00+08:00", "expected_headcount": 80,
                "posts": [{"post": "引导岗", "required_cert": "volunteer_basic",
                           "actor_id": "pl1"}],
                "depends_on": [], "rain_backup_site_id": None}
        status, body = call(self.service, "POST", "/plans",
                            {"request_id": "plan-submit", "plan_id": "plan1",
                             "name": "中秋", "units": [unit]}, "pl1")
        self.assertEqual(200, status)

        status, gate = call(self.service, "GET", "/plans/gate?plan_id=plan1")
        self.assertEqual(200, status)
        self.assertFalse(gate["releasable"])

        # 策划人不能签署安全检查项。
        status, body = call(self.service, "POST", "/plans/sign",
                            {"request_id": "sign-deny", "plan_id": "plan1",
                             "check_id": "fire-plaza"}, "pl1")
        self.assertEqual(403, status)

        status, body = call(self.service, "POST", "/plans/sign",
                            {"request_id": "sign-ok", "plan_id": "plan1",
                             "check_id": "fire-plaza"}, "sf1")
        self.assertEqual(200, status)

        # 签署未齐不能发布。
        status, gate = call(self.service, "GET", "/plans/gate?plan_id=plan1")
        self.assertTrue(gate["releasable"])
        status, published = call(self.service, "POST", "/plans/publish",
                                 {"request_id": "plan-publish", "plan_id": "plan1"}, "pl1")
        self.assertEqual(200, status)
        self.assertEqual("published", published["status"])

    def test_capacity_and_release_board(self):
        unit = {"key": "poetry", "name": "诗会", "site_id": "plaza",
                "start_time": "2026-09-27T18:00:00+08:00",
                "end_time": "2026-09-27T19:00:00+08:00", "expected_headcount": 500,
                "posts": [], "depends_on": [], "rain_backup_site_id": None}
        call(self.service, "POST", "/plans",
             {"request_id": "plan-over", "plan_id": "plan2", "name": "超载",
              "units": [unit]}, "pl1")
        status, gate = call(self.service, "GET", "/plans/gate?plan_id=plan2")
        self.assertTrue(any(b["code"] == "site_headcount_exceeded"
                            for u in gate["units"].values() for b in u["blockers"]))
        status, board = call(self.service, "GET", "/release-board")
        self.assertEqual(200, status)
        self.assertTrue(any(i["plan_id"] == "plan2" for i in board["items"]))
        status, ledger = call(self.service, "GET", "/capacity-ledger?site_id=plaza")
        self.assertEqual(200, status)
        self.assertEqual(200, ledger["max_headcount"])

    def test_rain_and_lifecycle_over_http(self):
        unit = {"key": "poetry", "name": "诗会", "site_id": "plaza",
                "start_time": "2026-09-27T18:00:00+08:00",
                "end_time": "2026-09-27T19:00:00+08:00", "expected_headcount": 80,
                "posts": [], "depends_on": [], "rain_backup_site_id": None}
        call(self.service, "POST", "/plans",
             {"request_id": "plan-submit", "plan_id": "plan1", "name": "中秋",
              "units": [unit]}, "pl1")
        call(self.service, "POST", "/plans/sign",
             {"request_id": "sign-ok", "plan_id": "plan1", "check_id": "fire-plaza"}, "sf1")
        call(self.service, "POST", "/plans/publish",
             {"request_id": "plan-publish", "plan_id": "plan1"}, "pl1")
        status, body = call(self.service, "POST", "/units/start",
                            {"request_id": "unit-start", "plan_id": "plan1",
                             "unit_key": "poetry"}, "pl1")
        self.assertEqual(200, status)
        self.assertEqual("in_progress", body["state"])
        status, rain = call(self.service, "POST", "/incidents/rain",
                            {"request_id": "rain-1", "incident_id": "inc-rain"}, "pl1")
        self.assertEqual(200, status)
        self.assertEqual("poetry", rain["manual_units"][0]["unit_key"])
        status, impact = call(self.service, "GET", "/impact-analysis?plan_id=plan1")
        self.assertEqual(200, status)
        self.assertEqual("manual_handling", impact["manual_units"][0]["state"])
        # 越权顺序操作：未结束不能取消。
        status, body = call(self.service, "POST", "/units/cancel",
                            {"request_id": "unit-cancel", "plan_id": "plan1",
                             "unit_key": "poetry"}, "pl1")
        self.assertEqual(409, status)
        status, body = call(self.service, "POST", "/units/manual-resolve",
                            {"request_id": "unit-resolve", "plan_id": "plan1",
                             "unit_key": "poetry", "decision": "end"}, "sf1")
        self.assertEqual(200, status)
        self.assertEqual("ended", body["state"])

    def test_missing_plan_id_is_400(self):
        status, body = call(self.service, "GET", "/plans/gate")
        self.assertEqual(400, status)


if __name__ == "__main__":
    unittest.main()
