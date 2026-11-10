"""PayPal Orders API integration for approved procurement purchases.

All credentials remain on the server. The existing payment_records table is
extended additively at startup so the original schema and seeded data survive.
"""

from __future__ import annotations

import hashlib
import logging
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import duckdb
import requests
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DB_PATH = Path(os.getenv("DATABASE_PATH", BASE_DIR / "database" / "procurement.duckdb"))
PAYPAL_BASE_URL = os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com").rstrip("/")
logger = logging.getLogger(__name__)


class PayPalOrderError(RuntimeError):
    """An expected PayPal integration or purchase workflow failure."""


def _connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(DB_PATH))
    ensure_payment_columns(con)
    return con


def ensure_payment_columns(con: duckdb.DuckDBPyConnection) -> None:
    """Apply a safe additive migration for PayPal Orders data."""
    existing = {row[1] for row in con.execute("PRAGMA table_info('payment_records')").fetchall()}
    columns = {
        "paypal_order_id": "VARCHAR",
        "paypal_capture_id": "VARCHAR",
        "approval_url": "VARCHAR",
        "last_error": "VARCHAR",
        "updated_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    }
    for name, sql_type in columns.items():
        if name not in existing:
            con.execute(f"ALTER TABLE payment_records ADD COLUMN {name} {sql_type}")


def _credentials() -> tuple[str, str]:
    # Load at call time so deployments/tests may inject environment variables.
    load_dotenv(BASE_DIR / ".env")
    client_id = os.getenv("PAYPAL_CLIENT_ID")
    client_secret = os.getenv("PAYPAL_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise PayPalOrderError("PayPal Sandbox credentials are not configured.")
    return client_id, client_secret


def _token(session: requests.Session | None = None) -> str:
    client_id, client_secret = _credentials()
    client = session or requests
    try:
        response = client.post(
            f"{PAYPAL_BASE_URL}/v1/oauth2/token",
            headers={"Accept": "application/json", "Accept-Language": "en_US"},
            data={"grant_type": "client_credentials"},
            auth=HTTPBasicAuth(client_id, client_secret),
            timeout=20,
        )
        response.raise_for_status()
        token = response.json().get("access_token")
        if not token:
            raise PayPalOrderError("PayPal authentication response did not include an access token.")
        return token
    except requests.RequestException as exc:
        logger.warning("PayPal OAuth request failed (status=%s)",
                       exc.response.status_code if exc.response is not None else "unavailable")
        raise PayPalOrderError("Could not authenticate with PayPal Sandbox.") from exc


def _request_id(idempotency_key: str, suffix: str = "create") -> str:
    # Stable, opaque and within PayPal's 38 character PayPal-Request-Id limit.
    digest = hashlib.sha256(f"{idempotency_key}:{suffix}".encode()).hexdigest()[:32]
    return f"proc-{digest}"


def _json_request(method: str, path: str, token: str, *, request_id: str | None = None,
                  payload: dict[str, Any] | None = None,
                  session: requests.Session | None = None) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if request_id:
        headers["PayPal-Request-Id"] = request_id
    client = session or requests
    try:
        response = client.request(method, f"{PAYPAL_BASE_URL}{path}", headers=headers,
                                  json=payload, timeout=30)
        response.raise_for_status()
        result = response.json() if response.content else {}
        if not isinstance(result, dict):
            raise PayPalOrderError("PayPal returned an invalid response.")
        return result
    except requests.RequestException as exc:
        logger.warning("PayPal API request failed for %s (status=%s)", path,
                       exc.response.status_code if exc.response is not None else "unavailable")
        raise PayPalOrderError("PayPal request failed; inspect the server log correlation entry.") from exc
    except ValueError as exc:
        logger.warning("PayPal API returned invalid JSON for %s", path)
        raise PayPalOrderError("PayPal returned an invalid response.") from exc


def _approved_payment(con: duckdb.DuckDBPyConnection, po_id: str) -> dict[str, Any]:
    row = con.execute("""
        SELECT p.payment_id, p.supplier_id, p.amount, p.idempotency_key,
               p.payment_status, p.paypal_order_id, p.approval_url,
               po.status AS po_status, po.business_id, po.total_amount,
               po.supplier_id AS po_supplier_id
        FROM payment_records p JOIN purchase_orders po USING (po_id)
        WHERE p.po_id = ?
        ORDER BY p.created_at DESC LIMIT 1
    """, [po_id]).fetchone()
    if not row:
        raise PayPalOrderError(f"No payment record exists for purchase order {po_id}.")
    keys = ("payment_id", "supplier_id", "amount", "idempotency_key", "payment_status",
            "paypal_order_id", "approval_url", "po_status", "business_id", "po_amount", "po_supplier_id")
    result = dict(zip(keys, row))
    try:
        if (Decimal(str(result["amount"])) != Decimal(str(result["po_amount"]))
                or result["supplier_id"] != result["po_supplier_id"]):
            raise PayPalOrderError("Purchase order and payment record do not match.")
    except (InvalidOperation, TypeError) as exc:
        raise PayPalOrderError("Purchase order amount is invalid.") from exc
    if result["po_status"] not in {"APPROVED", "PAID"} or result["payment_status"] not in {
        "APPROVED", "ORDER_CREATED", "CHECKOUT_PENDING", "CAPTURE_FAILED", "CANCELLED", "CAPTURED"
    }:
        raise PayPalOrderError("The purchase order must be explicitly approved before checkout.")
    return result


def approve_purchase(po_id: str, *, session: requests.Session | None = None,
                     return_url: str | None = None, cancel_url: str | None = None) -> dict[str, Any]:
    """Record an explicit approval, then create/reuse the corresponding PayPal order."""
    with duckdb.connect(str(DB_PATH)) as con:
        ensure_payment_columns(con)
        row = con.execute("""
            SELECT p.payment_id, p.payment_status, p.paypal_order_id, po.status
            FROM payment_records p JOIN purchase_orders po USING (po_id)
            WHERE p.po_id = ? ORDER BY p.created_at DESC LIMIT 1
        """, [po_id]).fetchone()
        if not row:
            raise PayPalOrderError(f"No payment record exists for purchase order {po_id}.")
        payment_id, payment_status, order_id, po_status = row
        if payment_status == "CAPTURED":
            return get_transaction_status(po_id)
        if order_id:
            return create_order(po_id, session=session, return_url=return_url, cancel_url=cancel_url)
        if po_status == "REJECTED" or payment_status == "REJECTED":
            raise PayPalOrderError("This purchase order was rejected and cannot be checked out.")
        if po_status not in ("PENDING", "APPROVED"):
            raise PayPalOrderError(f"Purchase order is {po_status}; it cannot be approved.")
        con.execute("UPDATE purchase_orders SET status = 'APPROVED' WHERE po_id = ?", [po_id])
        con.execute("""UPDATE payment_records SET payment_status = 'APPROVED', last_error = NULL,
                      updated_at = CURRENT_TIMESTAMP WHERE payment_id = ?""", [payment_id])
    return create_order(po_id, session=session, return_url=return_url, cancel_url=cancel_url)


def reject_purchase(po_id: str) -> dict[str, str]:
    """Persist an explicit rejection; rejected purchases cannot reach checkout."""
    with duckdb.connect(str(DB_PATH)) as con:
        ensure_payment_columns(con)
        row = con.execute("""
            SELECT p.payment_id, p.payment_status, p.paypal_order_id, po.status
            FROM payment_records p JOIN purchase_orders po USING (po_id)
            WHERE p.po_id = ? ORDER BY p.created_at DESC LIMIT 1
        """, [po_id]).fetchone()
        if not row:
            raise PayPalOrderError(f"No payment record exists for purchase order {po_id}.")
        payment_id, status, order_id, po_status = row
        if status == "CAPTURED" or order_id:
            raise PayPalOrderError("An order has already been created; rejection cannot cancel checkout.")
        if po_status not in ("PENDING", "REJECTED"):
            raise PayPalOrderError(f"Purchase order is {po_status}; it cannot be rejected.")
        con.execute("UPDATE purchase_orders SET status = 'REJECTED' WHERE po_id = ?", [po_id])
        con.execute("UPDATE payment_records SET payment_status = 'REJECTED', updated_at = CURRENT_TIMESTAMP WHERE payment_id = ?", [payment_id])
    return {"po_id": po_id, "status": "REJECTED"}


def create_order(po_id: str, *, session: requests.Session | None = None,
                 return_url: str | None = None, cancel_url: str | None = None) -> dict[str, Any]:
    """Create a PayPal Checkout order for an approved PO; retries reuse its order."""
    with duckdb.connect(str(DB_PATH)) as con:
        ensure_payment_columns(con)
        payment = _approved_payment(con, po_id)
        if payment["paypal_order_id"]:
            return {"po_id": po_id, "order_id": payment["paypal_order_id"],
                    "approval_url": payment["approval_url"], "status": payment["payment_status"]}

    amount = Decimal(str(payment["amount"]))
    if not amount.is_finite() or amount <= 0:
        raise PayPalOrderError("Purchase amount must be a positive monetary value.")
    try:
        token = _token(session)
        payload = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": po_id,
                "custom_id": payment["payment_id"],
                "description": f"Procurement purchase {po_id}",
                "amount": {"currency_code": "USD", "value": f"{amount:.2f}"},
            }],
            "application_context": {
                "brand_name": os.getenv("PAYPAL_BRAND_NAME", "Procurement Agent"),
                "user_action": "PAY_NOW",
                "return_url": return_url or os.getenv("PAYPAL_RETURN_URL", "http://localhost:8501/?checkout=success"),
                "cancel_url": cancel_url or os.getenv("PAYPAL_CANCEL_URL", "http://localhost:8501/?checkout=cancelled"),
            },
        }
        data = _json_request("POST", "/v2/checkout/orders", token,
                             request_id=_request_id(payment["idempotency_key"]),
                             payload=payload, session=session)
        order_id = data.get("id")
        units = data.get("purchase_units", [])
        if not isinstance(units, list) or any(not isinstance(item, dict) for item in units):
            raise PayPalOrderError("PayPal order response failed identity or amount validation.")
        unit = next((item for item in units if item.get("reference_id") == po_id), None)
        returned_amount = (unit or {}).get("amount", {})
        try:
            amount_matches = (returned_amount.get("currency_code") == "USD"
                              and Decimal(str(returned_amount.get("value", "NaN"))) == amount)
        except InvalidOperation:
            amount_matches = False
        if (not isinstance(order_id, str) or not order_id or len(order_id) > 128
                or data.get("status") != "CREATED" or len(units) != 1 or not unit
                or unit.get("custom_id") != payment["payment_id"] or not amount_matches):
            raise PayPalOrderError("PayPal order response failed identity or amount validation.")
        links = data.get("links", [])
        if not isinstance(links, list):
            raise PayPalOrderError("PayPal returned an invalid approval link.")
        approval_url = next((link.get("href") for link in links
                             if isinstance(link, dict) and link.get("rel") == "approve"), None)
        if approval_url:
            parsed_approval_url = urlsplit(approval_url)
            domain = (parsed_approval_url.hostname or "").lower()
            if parsed_approval_url.scheme != "https" or not (domain == "paypal.com" or domain.endswith(".paypal.com")):
                raise PayPalOrderError("PayPal returned an invalid approval link.")
        with duckdb.connect(str(DB_PATH)) as con:
            ensure_payment_columns(con)
            con.execute("""UPDATE payment_records SET paypal_order_id = ?, paypal_batch_id = ?,
                          approval_url = ?, payment_status = 'CHECKOUT_PENDING', last_error = NULL,
                          updated_at = CURRENT_TIMESTAMP WHERE payment_id = ?""",
                        [order_id, order_id, approval_url, payment["payment_id"]])
        return {"po_id": po_id, "order_id": order_id, "approval_url": approval_url,
                "status": data.get("status", "CREATED")}
    except (PayPalOrderError, InvalidOperation) as exc:
        with duckdb.connect(str(DB_PATH)) as con:
            ensure_payment_columns(con)
            con.execute("UPDATE payment_records SET last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE payment_id = ?",
                        [str(exc)[:1000], payment["payment_id"]])
        raise


def capture_order(po_id: str, *, session: requests.Session | None = None) -> dict[str, Any]:
    """Capture a buyer-approved order, safely reusing PayPal's capture idempotency key."""
    with duckdb.connect(str(DB_PATH)) as con:
        ensure_payment_columns(con)
        payment = _approved_payment(con, po_id)
        if not payment["paypal_order_id"]:
            raise PayPalOrderError("Create a PayPal order before capturing payment.")
        status_row = con.execute("SELECT paypal_capture_id FROM payment_records WHERE payment_id = ?",
                                 [payment["payment_id"]]).fetchone()
        capture_id = status_row[0]
        if payment["payment_status"] == "CAPTURED":
            return {"po_id": po_id, "order_id": payment["paypal_order_id"],
                    "capture_id": capture_id, "status": "COMPLETED"}
    try:
        token = _token(session)
        data = _json_request("POST", f"/v2/checkout/orders/{payment['paypal_order_id']}/capture",
                             token, request_id=_request_id(payment["idempotency_key"], "capture"),
                             payload={}, session=session)
        if data.get("id") != payment["paypal_order_id"]:
            raise PayPalOrderError("PayPal capture response did not match the stored order.")
        response_units = data.get("purchase_units", [])
        if not isinstance(response_units, list) or any(not isinstance(unit, dict) for unit in response_units):
            raise PayPalOrderError("PayPal capture response failed amount or identity validation.")
        matching_unit = next((unit for unit in response_units
                              if unit.get("reference_id") == po_id), None)
        payments = matching_unit.get("payments", {}) if matching_unit else {}
        captures = payments.get("captures", []) if isinstance(payments, dict) else []
        if not isinstance(captures, list) or any(not isinstance(item, dict) for item in captures):
            raise PayPalOrderError("PayPal capture response failed amount or identity validation.")
        capture = next(iter(captures), {})
        result_status = data.get("status", "UNKNOWN")
        new_capture_id = capture.get("id")
        if result_status == "COMPLETED":
            captured_amount = capture.get("amount", {})
            try:
                amount_matches = Decimal(str(captured_amount.get("value", "NaN"))) == Decimal(str(payment["amount"]))
            except InvalidOperation:
                amount_matches = False
            if (not new_capture_id or capture.get("status") != "COMPLETED"
                    or captured_amount.get("currency_code") != "USD" or not amount_matches):
                raise PayPalOrderError("PayPal capture response failed amount or identity validation.")
        with duckdb.connect(str(DB_PATH)) as con:
            ensure_payment_columns(con)
            if result_status == "COMPLETED":
                con.execute("""UPDATE payment_records SET paypal_capture_id = ?, payment_status = 'CAPTURED',
                              last_error = NULL, updated_at = CURRENT_TIMESTAMP WHERE payment_id = ?""",
                            [new_capture_id, payment["payment_id"]])
                con.execute("UPDATE purchase_orders SET status = 'PAID' WHERE po_id = ?", [po_id])
            else:
                con.execute("UPDATE payment_records SET payment_status = 'CAPTURE_FAILED', updated_at = CURRENT_TIMESTAMP WHERE payment_id = ?",
                            [payment["payment_id"]])
        return {"po_id": po_id, "order_id": payment["paypal_order_id"],
                "capture_id": new_capture_id, "status": result_status}
    except PayPalOrderError as exc:
        with duckdb.connect(str(DB_PATH)) as con:
            ensure_payment_columns(con)
            con.execute("""UPDATE payment_records SET payment_status = 'CAPTURE_FAILED', last_error = ?,
                          updated_at = CURRENT_TIMESTAMP WHERE payment_id = ?""",
                        [str(exc)[:1000], payment["payment_id"]])
        raise


def get_transaction_status(po_id: str) -> dict[str, Any]:
    with duckdb.connect(str(DB_PATH), read_only=True) as con:
        columns = {row[1] for row in con.execute("PRAGMA table_info('payment_records')").fetchall()}
        if "paypal_order_id" not in columns:
            return {"po_id": po_id, "status": "AWAITING_APPROVAL", "order_id": None,
                    "capture_id": None, "approval_url": None}
        row = con.execute("""
            SELECT p.payment_status, p.paypal_order_id, p.paypal_capture_id,
                   p.approval_url, p.last_error, po.status
            FROM payment_records p JOIN purchase_orders po USING (po_id)
            WHERE po.po_id = ? ORDER BY p.created_at DESC LIMIT 1
        """, [po_id]).fetchone()
        if not row:
            raise PayPalOrderError(f"No payment record exists for purchase order {po_id}.")
        return {"po_id": po_id, "status": row[0], "order_id": row[1],
                "capture_id": row[2], "approval_url": row[3],
                "purchase_order_status": row[5]}


def cancel_checkout(po_id: str) -> dict[str, str]:
    """Record a buyer cancelling PayPal checkout; a later explicit retry may recreate it."""
    with duckdb.connect(str(DB_PATH)) as con:
        ensure_payment_columns(con)
        row = con.execute("""SELECT p.payment_id, p.payment_status, po.status
            FROM payment_records p JOIN purchase_orders po USING (po_id)
            WHERE p.po_id = ? ORDER BY p.created_at DESC LIMIT 1""", [po_id]).fetchone()
        if not row:
            raise PayPalOrderError(f"No payment record exists for purchase order {po_id}.")
        payment_id, status, po_status = row
        if status == "CAPTURED":
            raise PayPalOrderError("A completed payment cannot be cancelled.")
        con.execute("UPDATE payment_records SET payment_status = 'CANCELLED', updated_at = CURRENT_TIMESTAMP WHERE payment_id = ?", [payment_id])
        con.execute("UPDATE purchase_orders SET status = 'APPROVED' WHERE po_id = ? AND status <> 'PAID'", [po_id])
    return {"po_id": po_id, "status": "CANCELLED"}
