"""Offline tests for approval gating, idempotency and order persistence."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import duckdb

import paypal_orders as orders


class PayPalOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "test.duckdb"
        self.db_path_patch = patch.object(orders, "DB_PATH", self.db)
        self.db_path_patch.start()
        with duckdb.connect(str(self.db)) as con:
            con.execute((orders.BASE_DIR / "database" / "schema.sql").read_text(encoding="utf-8"))
            con.execute("INSERT INTO business_accounts VALUES ('B1','Demo','USD',CURRENT_TIMESTAMP)")
            con.execute("INSERT INTO purchase_orders VALUES ('PO1','B1','S1',10,'2026-11-01',NULL,'PENDING',NULL)")
            con.execute("INSERT INTO payment_records (payment_id,po_id,supplier_id,amount,idempotency_key,payment_status) VALUES ('PAY1','PO1','S1',10,'stable-key','AWAITING_APPROVAL')")
            orders.ensure_payment_columns(con)

    def tearDown(self):
        self.db_path_patch.stop()
        self.temp.cleanup()

    def test_order_cannot_be_created_before_explicit_approval(self):
        with self.assertRaisesRegex(orders.PayPalOrderError, "explicitly approved"):
            orders.create_order("PO1")

    def test_order_request_persists_id_and_repeat_reuses_it(self):
        with duckdb.connect(str(self.db)) as con:
            con.execute("UPDATE purchase_orders SET status='APPROVED' WHERE po_id='PO1'")
            con.execute("UPDATE payment_records SET payment_status='APPROVED' WHERE po_id='PO1'")
        fake_session = object()
        with patch.object(orders, "_token", return_value="token"), patch.object(
            orders, "_json_request", return_value={
                "id": "ORDER-123", "status": "CREATED",
                "purchase_units": [{
                    "reference_id": "PO1", "custom_id": "PAY1",
                    "amount": {"currency_code": "USD", "value": "10.00"},
                }],
                "links": [{"rel": "approve", "href": "https://www.sandbox.paypal.com/approve"}],
            }
        ) as request:
            result = orders.create_order("PO1", session=fake_session)
            again = orders.create_order("PO1", session=fake_session)
        self.assertEqual(result["order_id"], "ORDER-123")
        self.assertEqual(again["order_id"], "ORDER-123")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(orders.get_transaction_status("PO1")["status"], "CHECKOUT_PENDING")

    def test_rejection_blocks_approval(self):
        orders.reject_purchase("PO1")
        with self.assertRaisesRegex(orders.PayPalOrderError, "rejected"):
            orders.approve_purchase("PO1")

    def test_request_id_is_stable_and_short_enough(self):
        first = orders._request_id("same-key")
        self.assertEqual(first, orders._request_id("same-key"))
        self.assertLessEqual(len(first), 38)

    def test_capture_response_must_match_order_and_amount_before_marking_paid(self):
        with duckdb.connect(str(self.db)) as con:
            con.execute("UPDATE purchase_orders SET status='APPROVED' WHERE po_id='PO1'")
            con.execute("UPDATE payment_records SET payment_status='APPROVED', paypal_order_id='ORDER-1' WHERE po_id='PO1'")
        response = {"id": "ORDER-1", "status": "COMPLETED", "purchase_units": [{
            "reference_id": "PO1", "payments": {"captures": [{
                "id": "CAP-1", "status": "COMPLETED",
                "amount": {"currency_code": "USD", "value": "999.00"},
            }]},
        }]}
        with patch.object(orders, "_token", return_value="token"), patch.object(
                orders, "_json_request", return_value=response):
            with self.assertRaisesRegex(orders.PayPalOrderError, "validation"):
                orders.capture_order("PO1")
        with duckdb.connect(str(self.db), read_only=True) as con:
            self.assertEqual(con.execute("SELECT status FROM purchase_orders WHERE po_id='PO1'").fetchone()[0], "APPROVED")
            self.assertEqual(con.execute("SELECT payment_status FROM payment_records WHERE po_id='PO1'").fetchone()[0], "CAPTURE_FAILED")


if __name__ == "__main__":
    unittest.main()
