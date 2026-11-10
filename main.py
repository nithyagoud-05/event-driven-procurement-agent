"""FastAPI application for the procurement agent and PayPal checkout."""

from __future__ import annotations

import logging
import os
import hmac
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import duckdb
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

import paypal_orders
from agent_workflow import AgentWorkflow

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DB_PATH = Path(os.getenv("DATABASE_PATH", BASE_DIR / "database" / "procurement.duckdb"))
_workflow: AgentWorkflow | None = None
logger = logging.getLogger("procurement_api")
_auth_failures: dict[str, deque[float]] = {}
_auth_failure_lock = threading.Lock()
_AUTH_FAILURE_WINDOW_SECONDS = 300
_AUTH_FAILURE_LIMIT = 10


def initialize_runtime_database() -> None:
    """Create and seed a fresh deployment database only when it does not exist."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists() and DB_PATH.stat().st_size > 0:
        return
    schema = (BASE_DIR / "database" / "schema.sql").read_text(encoding="utf-8")
    with duckdb.connect(str(DB_PATH)) as con:
        con.execute(schema)
    # Reuse the checked-in demo seeder only for a newly created empty deployment DB.
    import seed_data
    old_path = seed_data.DB_PATH
    seed_data.DB_PATH = DB_PATH
    try:
        seed_data.seed_database()
    finally:
        seed_data.DB_PATH = old_path


def get_workflow() -> AgentWorkflow:
    global _workflow
    if _workflow is None:
        _workflow = AgentWorkflow(db_path=DB_PATH)
    return _workflow


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _workflow
    initialize_runtime_database()
    get_workflow()
    yield
    if _workflow is not None:
        _workflow.close()
        _workflow = None


app = FastAPI(title="Liquidity-Aware Procurement Agent", version="1.0.0", lifespan=lifespan)
allowed_origins = [origin.strip() for origin in os.getenv(
    "CORS_ALLOWED_ORIGINS", "http://localhost:8501,http://127.0.0.1:8501"
).split(",") if origin.strip()]
app.add_middleware(CORSMiddleware, allow_origins=allowed_origins,
                   allow_credentials=False, allow_methods=["GET", "POST"], allow_headers=["*"])


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    business_id: str = Field(default="BUS001", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    run_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(pattern="^(approved|rejected)$")


def _workflow_call(callable_, *args, **kwargs):
    try:
        return callable_(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (duckdb.Error, OSError) as exc:
        logger.exception("Workflow storage operation failed")
        raise HTTPException(status_code=500, detail="Workflow storage operation failed") from exc


def _require_approval_token(token: str | None, request: Request | None = None) -> None:
    expected = os.getenv("APPROVAL_API_TOKEN")
    if not expected or len(expected) < 32:
        raise HTTPException(status_code=503, detail="Owner API authentication is not configured")
    supplied = (token or "")
    valid = len(supplied) <= 256 and hmac.compare_digest(
        supplied.encode("utf-8"), expected.encode("utf-8")
    )
    if not valid:
        source = request.client.host if request and request.client else "unknown"
        now = time.monotonic()
        with _auth_failure_lock:
            failures = _auth_failures.setdefault(source, deque())
            while failures and now - failures[0] > _AUTH_FAILURE_WINDOW_SECONDS:
                failures.popleft()
            if len(failures) >= _AUTH_FAILURE_LIMIT:
                raise HTTPException(status_code=429, detail="Too many failed authentication attempts")
            failures.append(now)
        raise HTTPException(status_code=401, detail="A valid X-Approval-Token is required")


def _dashboard_data(business_id: str) -> dict[str, Any]:
    if not DB_PATH.exists():
        raise HTTPException(status_code=503, detail="Database is not initialized")
    with duckdb.connect(str(DB_PATH), read_only=True) as con:
        business = con.execute("SELECT business_name, currency FROM business_accounts WHERE business_id = ?",
                               [business_id]).fetchone()
        if not business:
            raise HTTPException(status_code=404, detail=f"Unknown business: {business_id}")
        cash_rows = con.execute("SELECT transaction_type, amount FROM cash_ledger WHERE business_id=? AND upper(status)='COMPLETED'",
                                [business_id]).fetchall()
        cash = Decimal("0.00")
        for kind, amount in cash_rows:
            amount = abs(Decimal(str(amount)))
            if any(word in (kind or "").upper() for word in ("OUT", "DEBIT", "EXPENSE", "PAYMENT", "WITHDRAW")):
                cash -= amount
            else:
                cash += amount
        payroll = con.execute("""SELECT COALESCE(SUM(amount),0) FROM mandatory_obligations
            WHERE business_id=? AND upper(status)='PENDING' AND upper(obligation_type)='PAYROLL'""",
                              [business_id]).fetchone()[0]
        receipts = con.execute("""SELECT receivable_id, customer_name, expected_amount, expected_date,
            status, actual_date FROM receivables WHERE business_id=? ORDER BY expected_date""",
                               [business_id]).fetchall()
        inventory = con.execute("""SELECT product_id,product_name,current_stock,daily_demand,safety_stock,
            unit_cost FROM inventory WHERE business_id=? ORDER BY product_id""", [business_id]).fetchall()
        supplier_rows = con.execute("""SELECT q.quote_id,q.product_id,i.product_name,s.supplier_id,
            s.supplier_name,q.unit_price,q.minimum_order_quantity,q.available_quantity,q.lead_time_days,
            q.quote_valid_until FROM supplier_quotes q JOIN inventory i ON i.product_id=q.product_id
            JOIN suppliers s ON s.supplier_id=q.supplier_id WHERE i.business_id=? AND s.is_active=TRUE
            ORDER BY q.product_id,q.unit_price,q.lead_time_days""", [business_id]).fetchall()
        open_orders = con.execute("""SELECT po.po_id,po.supplier_id,s.supplier_name,po.total_amount,
            po.status,po.expected_delivery FROM purchase_orders po LEFT JOIN suppliers s USING(supplier_id)
            WHERE po.business_id=? AND upper(po.status) NOT IN ('PAID','REJECTED') ORDER BY po.order_date DESC""",
                                  [business_id]).fetchall()

    overdue, delayed = [], []
    for row in receipts:
        receipt = {"receivable_id": row[0], "customer_name": row[1], "amount": f"{row[2]:.2f}",
                   "expected_date": row[3].isoformat(), "status": row[4],
                   "actual_date": row[5].isoformat() if row[5] else None}
        if str(row[4]).upper() == "DELAYED":
            delayed.append(receipt)
        elif str(row[4]).upper() == "PENDING" and row[3] is not None and row[5] is None and row[3] < date.today():
            receipt["status"] = "OVERDUE"
            overdue.append(receipt)
    return {
        "business_id": business_id,
        "business_name": business[0],
        "currency": business[1],
        "cash": f"{cash:.2f}",
        "reserved_payroll": f"{Decimal(str(payroll)):.2f}",
        "cash_after_payroll": f"{cash - Decimal(str(payroll)):.2f}",
        "delayed_receivables": delayed,
        "overdue_receivables": overdue,
        "expected_receivables": [
            {"receivable_id": r[0], "customer_name": r[1], "amount": f"{r[2]:.2f}",
             "expected_date": r[3].isoformat(), "status": r[4]}
            for r in receipts if str(r[4]).upper() == "PENDING" and r[5] is None and r[3] >= date.today()
        ],
        "inventory": [{"product_id": r[0], "product_name": r[1], "current_stock": r[2],
                       "daily_demand": r[3], "safety_stock": r[4], "unit_cost": f"{r[5]:.2f}" if r[5] is not None else None}
                      for r in inventory],
        "supplier_comparison": [{"quote_id": r[0], "product_id": r[1], "product_name": r[2],
                                 "supplier_id": r[3], "supplier_name": r[4], "unit_price": f"{r[5]:.2f}",
                                 "minimum_order_quantity": r[6], "available_quantity": r[7],
                                 "lead_time_days": r[8], "quote_valid_until": r[9].isoformat() if r[9] else None}
                                for r in supplier_rows],
        "purchase_orders": [{"po_id": r[0], "supplier_id": r[1], "supplier_name": r[2],
                             "amount": f"{r[3]:.2f}", "status": r[4],
                             "expected_delivery": r[5].isoformat() if r[5] else None} for r in open_orders],
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.middleware("http")
async def limit_api_request_size(request: Request, call_next):
    """Reject oversized JSON requests before endpoint parsing."""
    if request.url.path.startswith("/api/"):
        raw_length = request.headers.get("content-length")
        if raw_length:
            try:
                if int(raw_length) > 65_536:
                    return JSONResponse(status_code=413, content={"detail": "Request body too large"})
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"})
    return await call_next(request)


def _require_run_purchase(run_id: str, po_id: str) -> None:
    """Bind transaction access to a PO recorded in this approved workflow run."""
    state = _workflow_call(get_workflow().get_state, run_id)
    if po_id not in {po.get("po_id") for po in state.get("purchase_orders", [])}:
        raise HTTPException(status_code=404, detail="Transaction is not part of this workflow")
    if state.get("approval_decision") != "approved":
        raise HTTPException(status_code=409, detail="Workflow approval is required for this transaction")


@app.get("/api/dashboard/{business_id}")
def dashboard(business_id: str, request: Request, x_approval_token: str | None = Header(default=None)):
    _require_approval_token(x_approval_token, request)
    return _dashboard_data(business_id)


@app.post("/api/agent/runs")
def start_agent_run(body: RunRequest, request: Request, x_approval_token: str | None = Header(default=None)):
    _require_approval_token(x_approval_token, request)
    run_id = body.run_id or str(uuid4())
    state = _workflow_call(get_workflow().start, body.business_id, run_id)
    return state


@app.get("/api/agent/runs/{run_id}")
def get_agent_run(run_id: str, request: Request, x_approval_token: str | None = Header(default=None)):
    _require_approval_token(x_approval_token, request)
    return _workflow_call(get_workflow().get_state, run_id)


@app.get("/api/agent/runs/{run_id}/recommendation")
def get_agent_recommendation(run_id: str, request: Request, x_approval_token: str | None = Header(default=None)):
    _require_approval_token(x_approval_token, request)
    state = _workflow_call(get_workflow().get_state, run_id)
    return {"run_id": run_id, "status": state.get("optimizer_result", {}).get("status", "PENDING"),
            "recommendation": state.get("optimizer_result"),
            "explanation": state.get("explanation"),
            "purchase_orders": state.get("purchase_orders", [])}


@app.post("/api/agent/runs/{run_id}/approval")
def decide_agent_run(run_id: str, request: ApprovalRequest,
                     http_request: Request,
                     x_approval_token: str | None = Header(default=None)):
    _require_approval_token(x_approval_token, http_request)
    return _workflow_call(get_workflow().decide, run_id, request.decision, "authenticated_owner")


@app.get("/api/agent/runs/{run_id}/transactions")
def get_run_transactions(run_id: str, request: Request, x_approval_token: str | None = Header(default=None)):
    _require_approval_token(x_approval_token, request)
    state = _workflow_call(get_workflow().get_state, run_id)
    transactions = []
    for po in state.get("purchase_orders", []):
        try:
            transactions.append(paypal_orders.get_transaction_status(po["po_id"]))
        except paypal_orders.PayPalOrderError:
            transactions.append({"po_id": po["po_id"], "status": "UNKNOWN", "error": "Transaction status unavailable"})
    return {"run_id": run_id, "transactions": transactions}


@app.get("/api/transactions/{po_id}")
def get_transaction(po_id: str, run_id: str = Query(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
                    request: Request = None,
                    x_approval_token: str | None = Header(default=None)):
    _require_approval_token(x_approval_token, request)
    _require_run_purchase(run_id, po_id)
    try:
        return paypal_orders.get_transaction_status(po_id)
    except paypal_orders.PayPalOrderError as exc:
        raise HTTPException(status_code=404, detail="Transaction not found") from exc


@app.post("/api/transactions/{po_id}/capture")
def capture_transaction(po_id: str, run_id: str = Query(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
                        request: Request = None,
                        x_approval_token: str | None = Header(default=None)):
    _require_approval_token(x_approval_token, request)
    _require_run_purchase(run_id, po_id)
    try:
        return paypal_orders.capture_order(po_id)
    except paypal_orders.PayPalOrderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/transactions/{po_id}/cancel")
def cancel_transaction(po_id: str, run_id: str = Query(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
                       request: Request = None,
                       x_approval_token: str | None = Header(default=None)):
    _require_approval_token(x_approval_token, request)
    _require_run_purchase(run_id, po_id)
    try:
        return paypal_orders.cancel_checkout(po_id)
    except paypal_orders.PayPalOrderError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/transactions/{po_id}/create-order")
def retry_create_order(po_id: str, run_id: str = Query(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
                       request: Request = None,
                       x_approval_token: str | None = Header(default=None)):
    _require_approval_token(x_approval_token, request)
    _require_run_purchase(run_id, po_id)
    try:
        return paypal_orders.create_order(po_id)
    except paypal_orders.PayPalOrderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/purchase-orders/{po_id}/reject")
def reject_purchase_order(po_id: str, request: Request, x_approval_token: str | None = Header(default=None)):
    _require_approval_token(x_approval_token, request)
    try:
        return paypal_orders.reject_purchase(po_id)
    except paypal_orders.PayPalOrderError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

