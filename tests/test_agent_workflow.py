"""Integration checks for durable LangGraph approval and checkout lifecycle."""
import tempfile
import unittest
from contextlib import redirect_stdout
import io
from pathlib import Path
from unittest.mock import patch

import duckdb
from fastapi.testclient import TestClient

import paypal_orders
from agent_workflow import AgentWorkflow
import seed_data


class AgentWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "procurement.duckdb"
        self.checkpoints = self.root / "checkpoints.sqlite"
        with duckdb.connect(str(self.db)) as con:
            con.execute((seed_data.BASE_DIR / "database" / "schema.sql").read_text(encoding="utf-8"))
        original = seed_data.DB_PATH
        seed_data.DB_PATH = self.db
        try:
            with redirect_stdout(io.StringIO()):
                seed_data.seed_database()
        finally:
            seed_data.DB_PATH = original
        self.paypal_db = patch.object(paypal_orders, "DB_PATH", self.db)
        self.paypal_db.start()
        self.workflow = AgentWorkflow(self.db, self.checkpoints, gemini_api_key="")

    def tearDown(self):
        import main
        main._auth_failures.clear()
        self.workflow.close()
        self.paypal_db.stop()
        self.temp.cleanup()

    def test_run_pauses_with_persisted_human_approval(self):
        state = self.workflow.start("BUS001", "durable-approval")
        self.assertTrue(state["waiting_for_approval"])
        self.assertEqual(state["phase"], "AWAITING_APPROVAL")
        self.assertEqual(state["optimizer_result"]["total_procurement_cost"], "650.00")
        self.assertEqual(state["explanation_source"], "fallback")
        self.assertTrue(state["trigger_detected"])
        self.assertEqual(state["risk_analysis"]["risk_level"], "HIGH")
        self.assertEqual(state["risk_analysis"]["existing_pending_order_total"], "2000.00")
        self.assertEqual(state["risk_analysis"]["pending_order_budget_shortfall"], "500.00")

        self.workflow.close()
        self.workflow = AgentWorkflow(self.db, self.checkpoints, gemini_api_key="")
        restored = self.workflow.get_state("durable-approval")
        self.assertTrue(restored["waiting_for_approval"])
        self.assertEqual(restored["run_id"], "durable-approval")

    def test_rejection_records_state_and_does_not_call_paypal(self):
        state = self.workflow.start("BUS001", "reject-run")
        po_id = state["purchase_orders"][0]["po_id"]
        with patch.object(paypal_orders, "approve_purchase") as approve:
            result = self.workflow.decide("reject-run", "rejected")
        approve.assert_not_called()
        self.assertEqual(result["phase"], "REJECTED")
        with duckdb.connect(str(self.db), read_only=True) as con:
            self.assertEqual(con.execute("SELECT status FROM purchase_orders WHERE po_id=?", [po_id]).fetchone()[0], "REJECTED")
            self.assertEqual(con.execute("SELECT payment_status FROM payment_records WHERE po_id=?", [po_id]).fetchone()[0], "REJECTED")

    def test_approval_creates_paypal_order_once_and_is_idempotent(self):
        self.workflow.start("BUS001", "approve-run")
        def create_response(method, path, token, **kwargs):
            return {
                "id": "ORDER-DEMO", "status": "CREATED",
                "purchase_units": kwargs["payload"]["purchase_units"],
                "links": [{"rel": "approve", "href": "https://www.sandbox.paypal.com/approve"}],
            }
        with patch.object(paypal_orders, "_token", return_value="test-token"), patch.object(
            paypal_orders, "_json_request", side_effect=create_response
        ) as create:
            result = self.workflow.decide("approve-run", "approved", "owner@example.test")
            repeated = self.workflow.decide("approve-run", "approved", "owner@example.test")
        self.assertEqual(result["approval_decision"], "approved")
        self.assertEqual(result["phase"], "CHECKOUT_PENDING")
        self.assertEqual(result["checkout_results"][0]["order_id"], "ORDER-DEMO")
        self.assertEqual(repeated["checkout_results"][0]["order_id"], "ORDER-DEMO")
        self.assertEqual(create.call_count, 1)

    def test_api_end_to_end_recommend_approve_and_capture(self):
        import main

        expected_po = {"value": None}
        def paypal_response(method, path, token, **kwargs):
            if path.endswith("/capture"):
                return {"id": "ORDER-E2E", "status": "COMPLETED", "purchase_units": [{
                    "reference_id": expected_po["value"],
                    "payments": {"captures": [{"id": "CAPTURE-E2E", "status": "COMPLETED",
                                                  "amount": {"currency_code": "USD", "value": "650.00"}}]}
                }]}
            return {"id": "ORDER-E2E", "status": "CREATED",
                    "purchase_units": kwargs["payload"]["purchase_units"],
                    "links": [{"rel": "approve", "href": "https://www.sandbox.paypal.com/approve"}]}

        with patch.object(main, "DB_PATH", self.db), patch.object(main, "_workflow", self.workflow), \
             patch.object(paypal_orders, "_token", return_value="test-token"), patch.object(
                 paypal_orders, "_json_request", side_effect=paypal_response
             ):
            with patch.dict("os.environ", {"APPROVAL_API_TOKEN": "test-owner-token-which-is-long-enough-123"}):
              with TestClient(main.app, headers={"X-Approval-Token": "test-owner-token-which-is-long-enough-123"}) as client:
                dashboard = client.get("/api/dashboard/BUS001")
                self.assertEqual(dashboard.status_code, 200)
                self.assertEqual(dashboard.json()["cash"], "5000.00")

                started = client.post("/api/agent/runs", json={
                    "business_id": "BUS001", "run_id": "api-e2e-run",
                })
                self.assertEqual(started.status_code, 200, started.text)
                state = started.json()
                self.assertTrue(state["waiting_for_approval"])
                po_id = state["purchase_orders"][0]["po_id"]
                expected_po["value"] = po_id
                recommendation = client.get("/api/agent/runs/api-e2e-run/recommendation")
                self.assertEqual(recommendation.json()["recommendation"]["total_procurement_cost"], "650.00")

                approved = client.post("/api/agent/runs/api-e2e-run/approval", json={
                    "decision": "approved",
                })
                self.assertEqual(approved.status_code, 200, approved.text)
                self.assertEqual(approved.json()["phase"], "CHECKOUT_PENDING")

                captured = client.post(f"/api/transactions/{po_id}/capture", params={"run_id": "api-e2e-run"})
                self.assertEqual(captured.status_code, 200, captured.text)
                self.assertEqual(captured.json()["capture_id"], "CAPTURE-E2E")
                self.assertEqual(captured.json()["status"], "COMPLETED")

                transaction = client.get(f"/api/transactions/{po_id}", params={"run_id": "api-e2e-run"})
                self.assertEqual(transaction.json()["status"], "CAPTURED")
        self.workflow = AgentWorkflow(self.db, self.checkpoints, gemini_api_key="")

    def test_sensitive_api_routes_fail_closed_without_valid_token(self):
        import main

        with patch.dict("os.environ", {}, clear=True), TestClient(main.app) as client:
            self.assertEqual(client.get("/api/dashboard/BUS001").status_code, 503)
            self.assertEqual(client.post("/api/agent/runs", json={}).status_code, 503)
        with patch.dict("os.environ", {"APPROVAL_API_TOKEN": "test-owner-token-which-is-long-enough-123"}), \
             TestClient(main.app) as client:
            self.assertEqual(client.get("/api/dashboard/BUS001", headers={
                "X-Approval-Token": "wrong-token"
            }).status_code, 401)
            self.assertEqual(client.get("/health").status_code, 200)

    def test_repeated_bad_owner_tokens_are_rate_limited(self):
        import main

        token = "test-owner-token-which-is-long-enough-123"
        with patch.dict("os.environ", {"APPROVAL_API_TOKEN": token}), TestClient(main.app) as client:
            for _ in range(main._AUTH_FAILURE_LIMIT):
                self.assertEqual(client.get("/api/dashboard/BUS001", headers={
                    "X-Approval-Token": "incorrect"
                }).status_code, 401)
            self.assertEqual(client.get("/api/dashboard/BUS001", headers={
                "X-Approval-Token": "incorrect"
            }).status_code, 429)

    def test_api_does_not_accept_client_supplied_approval_actor(self):
        import main

        token = "test-owner-token-which-is-long-enough-123"
        with patch.dict("os.environ", {"APPROVAL_API_TOKEN": token}), TestClient(
                main.app, headers={"X-Approval-Token": token}) as client:
            response = client.post("/api/agent/runs/some-run/approval", json={
                "decision": "approved", "actor": "forged-owner"
            })
        self.assertEqual(response.status_code, 422)

    def test_cross_run_transaction_access_is_rejected(self):
        import main

        token = "test-owner-token-which-is-long-enough-123"
        with patch.object(main, "DB_PATH", self.db), patch.object(main, "_workflow", self.workflow), \
             patch.dict("os.environ", {"APPROVAL_API_TOKEN": token}), TestClient(
                 main.app, headers={"X-Approval-Token": token}) as client:
            first = client.post("/api/agent/runs", json={"business_id": "BUS001", "run_id": "run-alpha"}).json()
            second = client.post("/api/agent/runs", json={"business_id": "BUS001", "run_id": "run-beta"}).json()
            first_po = first["purchase_orders"][0]["po_id"]
            second_po = second["purchase_orders"][0]["po_id"]
            self.assertNotEqual(first_po, second_po)
            response = client.post(f"/api/transactions/{second_po}/capture", params={"run_id": "run-alpha"})
        self.assertEqual(response.status_code, 404)

    def test_api_rejects_oversized_request_content_length(self):
        import main

        token = "test-owner-token-which-is-long-enough-123"
        with patch.dict("os.environ", {"APPROVAL_API_TOKEN": token}), TestClient(
                main.app, headers={"X-Approval-Token": token}) as client:
            response = client.post("/api/agent/runs", content=b"{}", headers={
                "Content-Type": "application/json", "Content-Length": "65537",
                "X-Approval-Token": token,
            })
        self.assertEqual(response.status_code, 413)

    def test_deployment_initializes_and_seeds_only_a_new_database(self):
        import main

        fresh_db = self.root / "new-deployment.duckdb"
        with patch.object(main, "DB_PATH", fresh_db):
            with redirect_stdout(io.StringIO()):
                main.initialize_runtime_database()
                main.initialize_runtime_database()
        with duckdb.connect(str(fresh_db), read_only=True) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM inventory").fetchone()[0], 3)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM receivables WHERE status='DELAYED'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
