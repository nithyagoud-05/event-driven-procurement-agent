"""LangGraph procurement workflow with durable approval interrupts."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import duckdb
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

import paypal_orders
from procurement_optimizer import (
    OptimizationConfig,
    load_procurement_input,
    optimize_procurement,
)

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)


class ProcurementState(TypedDict, total=False):
    run_id: str
    business_id: str
    delayed_payments: list[dict[str, Any]]
    trigger_detected: bool
    risk_analysis: dict[str, Any]
    optimizer_result: dict[str, Any]
    explanation: str
    explanation_source: str
    purchase_orders: list[dict[str, Any]]
    approval_decision: str
    approved_by: str
    approval_at: str
    checkout_results: list[dict[str, Any]]
    phase: str
    error: str


class AgentWorkflow:
    """Owns graph compilation, SQLite checkpoints, and domain service nodes."""

    def __init__(self, db_path: str | Path | None = None,
                 checkpoint_path: str | Path | None = None,
                 gemini_api_key: str | None = None):
        self.db_path = Path(db_path or os.getenv("DATABASE_PATH", BASE_DIR / "database" / "procurement.duckdb"))
        self.checkpoint_path = Path(checkpoint_path or os.getenv(
            "LANGGRAPH_CHECKPOINT_PATH", BASE_DIR / "database" / "workflow-checkpoints.sqlite"))
        self.gemini_api_key = gemini_api_key if gemini_api_key is not None else os.getenv("GEMINI_API_KEY")
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self._sqlite = sqlite3.connect(str(self.checkpoint_path), check_same_thread=False)
        self._lock = threading.RLock()
        self._checkpointer = SqliteSaver(self._sqlite)
        self._checkpointer.setup()
        builder = StateGraph(ProcurementState)
        builder.add_node("trigger", self._trigger)
        builder.add_node("risk_analysis", self._risk_analysis)
        builder.add_node("optimizer", self._optimize)
        builder.add_node("synthesis", self._synthesize)
        builder.add_node("persist_recommendation", self._persist_recommendation)
        builder.add_node("human_approval", self._human_approval)
        builder.add_node("record_rejection", self._record_rejection)
        builder.add_node("create_checkout", self._create_checkout)
        builder.add_node("finish_without_purchase", self._finish_without_purchase)
        builder.add_edge(START, "trigger")
        builder.add_edge("trigger", "risk_analysis")
        builder.add_edge("risk_analysis", "optimizer")
        builder.add_edge("optimizer", "synthesis")
        builder.add_conditional_edges("synthesis", self._needs_approval, {
            "approval": "persist_recommendation",
            "finish": "finish_without_purchase",
        })
        builder.add_edge("persist_recommendation", "human_approval")
        builder.add_conditional_edges("human_approval", self._approval_route, {
            "approved": "create_checkout",
            "rejected": "record_rejection",
        })
        builder.add_edge("record_rejection", END)
        builder.add_edge("create_checkout", END)
        builder.add_edge("finish_without_purchase", END)
        self.graph = builder.compile(checkpointer=self._checkpointer)

    def close(self) -> None:
        self._sqlite.close()

    @staticmethod
    def _config(run_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": run_id}}

    def start(self, business_id: str, run_id: str | None = None) -> dict[str, Any]:
        run_id = run_id or str(uuid.uuid4())
        with self._lock:
            existing = self.graph.get_state(self._config(run_id))
            if existing.values:
                return self.get_state(run_id)
            self.graph.invoke({"run_id": run_id, "business_id": business_id, "phase": "STARTING"},
                              config=self._config(run_id))
            return self.get_state(run_id)

    def decide(self, run_id: str, decision: str, actor: str = "business_owner") -> dict[str, Any]:
        decision = decision.lower().strip()
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision must be 'approved' or 'rejected'")
        with self._lock:
            state = self.graph.get_state(self._config(run_id))
            if not state.values:
                raise KeyError(f"Unknown run: {run_id}")
            current = state.values
            if current.get("approval_decision"):
                if current["approval_decision"] != decision:
                    raise ValueError("This run already has a different recorded decision")
                return self.get_state(run_id)
            if "human_approval" not in state.next:
                raise ValueError("This workflow is not waiting for human approval")
            self.graph.invoke(Command(resume={
                "decision": decision,
                "actor": actor,
                "decided_at": date.today().isoformat(),
            }), config=self._config(run_id))
            return self.get_state(run_id)

    def get_state(self, run_id: str) -> dict[str, Any]:
        snapshot = self.graph.get_state(self._config(run_id))
        if not snapshot.values:
            raise KeyError(f"Unknown run: {run_id}")
        result = dict(snapshot.values)
        result["waiting_for_approval"] = "human_approval" in snapshot.next
        result["next"] = list(snapshot.next)
        return result

    def _trigger(self, state: ProcurementState) -> dict[str, Any]:
        con = duckdb.connect(str(self.db_path), read_only=True)
        try:
            rows = con.execute("""SELECT receivable_id, customer_name, expected_amount,
                expected_date, status FROM receivables WHERE business_id = ?
                AND (upper(status) = 'DELAYED' OR (upper(status) = 'PENDING' AND expected_date < CURRENT_DATE))
                ORDER BY expected_date""", [state["business_id"]]).fetchall()
            payments = [{
                "receivable_id": row[0], "customer_name": row[1],
                "amount": f"{row[2]:.2f}", "expected_date": row[3].isoformat(),
                "status": "DELAYED" if row[4].upper() == "DELAYED" else "OVERDUE",
            } for row in rows]
        finally:
            con.close()
        return {"delayed_payments": payments, "trigger_detected": bool(payments), "phase": "TRIGGERED"}

    def _optimization_config(self) -> OptimizationConfig:
        return OptimizationConfig(min_cash_buffer=os.getenv("MIN_CASH_BUFFER", "0.00"))

    def _risk_analysis(self, state: ProcurementState) -> dict[str, Any]:
        data = load_procurement_input(state["business_id"], db_path=self.db_path,
                                      config=self._optimization_config())
        cash = float(data.available_cash)
        payroll = float(data.reserved_payroll)
        buffer = float(data.config.min_cash_buffer)
        budget = max(0.0, cash - payroll - buffer)
        with duckdb.connect(str(self.db_path), read_only=True) as con:
            pending_orders = con.execute("""SELECT po_id,total_amount FROM purchase_orders
                WHERE business_id=? AND upper(status)='PENDING' ORDER BY order_date""",
                                         [state["business_id"]]).fetchall()
        pending_total = sum(float(row[1]) for row in pending_orders)
        pending_shortfall = max(0.0, pending_total - budget)
        risk_level = "HIGH" if pending_shortfall > 0 or (state.get("trigger_detected") and budget <= 0) else (
            "ELEVATED" if state.get("trigger_detected") else "NORMAL")
        pending_text = (
            f"There are ${pending_total:.2f} in existing unapproved purchase orders, "
            f"which is ${pending_shortfall:.2f} above the current procurement budget. "
            "They remain separate from this recommendation and require owner review. "
            if pending_orders else "There are no existing unapproved purchase orders. "
        )
        return {"risk_analysis": {
            "risk_level": risk_level,
            "available_cash": f"{cash:.2f}",
            "reserved_payroll": f"{payroll:.2f}",
            "minimum_cash_buffer": f"{buffer:.2f}",
            "procurement_budget": f"{budget:.2f}",
            "delayed_receivables": f"{float(data.delayed_receivables):.2f}",
            "delayed_receivables_available_for_spend": False,
            "existing_pending_purchase_orders": [
                {"po_id": row[0], "amount": f"{float(row[1]):.2f}"} for row in pending_orders
            ],
            "existing_pending_order_total": f"{pending_total:.2f}",
            "pending_order_budget_shortfall": f"{pending_shortfall:.2f}",
            "analysis": (
                f"Current cash is ${cash:.2f}; ${payroll:.2f} is reserved for payroll and "
                f"${buffer:.2f} is protected as the operating buffer. Procurement must fit within ${budget:.2f}. "
                f"${float(data.delayed_receivables):.2f} in delayed receivables is excluded until received. "
                + pending_text
            ),
        }, "phase": "RISK_ANALYZED"}

    def _optimize(self, state: ProcurementState) -> dict[str, Any]:
        data = load_procurement_input(state["business_id"], db_path=self.db_path,
                                      config=self._optimization_config())
        result = optimize_procurement(data)
        return {"optimizer_result": result, "phase": "OPTIMIZED"}

    def _synthesize(self, state: ProcurementState) -> dict[str, Any]:
        result = state["optimizer_result"]
        prompt = (
            "Explain this procurement optimization to a small business owner in plain language. "
            "Do not change quantities, prices, or solver conclusions. State that no purchase occurs without approval.\n"
            + json.dumps({"risk": state.get("risk_analysis"), "optimization": result}, default=str)
        )
        if self.gemini_api_key:
            try:
                from google import genai
                client = genai.Client(api_key=self.gemini_api_key)
                response = client.models.generate_content(
                    model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"), contents=prompt)
                text = (response.text or "").strip()
                if text:
                    return {"explanation": text, "explanation_source": "gemini", "phase": "EXPLAINED"}
            except Exception as exc:  # fallback keeps core decision and route usable
                logger.warning("Gemini explanation unavailable; using deterministic fallback: %s", exc)
        return {"explanation": self._fallback_explanation(state),
                "explanation_source": "fallback", "phase": "EXPLAINED"}

    @staticmethod
    def _fallback_explanation(state: ProcurementState) -> str:
        result = state["optimizer_result"]
        summary = result.get("explanation") or "The optimizer did not produce a valid purchase recommendation."
        risk = state.get("risk_analysis", {})
        return (f"{summary} The spend limit is ${risk.get('procurement_budget', '0.00')} after protecting "
                f"${risk.get('reserved_payroll', '0.00')} for payroll. Delayed customer payments are not included "
                "in available cash. A business owner must approve before any PayPal checkout is created.")

    @staticmethod
    def _needs_approval(state: ProcurementState) -> str:
        result = state.get("optimizer_result", {})
        return "approval" if result.get("recommendation_valid") and result.get("recommended_quantities") else "finish"

    def _persist_recommendation(self, state: ProcurementState) -> dict[str, Any]:
        result = state["optimizer_result"]
        run_id = state["run_id"]
        grouped: dict[str, dict[str, Any]] = {}
        for line in result.get("recommended_quantities", []):
            group = grouped.setdefault(line["supplier_id"], {
                "supplier_id": line["supplier_id"], "supplier_name": line["supplier_name"],
                "amount_cents": 0, "lead_time_days": 0, "lines": [],
            })
            group["amount_cents"] += line["line_total_cents"]
            group["lead_time_days"] = max(group["lead_time_days"], line["lead_time_days"])
            group["lines"].append(line)
        purchase_orders = []
        with duckdb.connect(str(self.db_path)) as con:
            for supplier_id, group in sorted(grouped.items()):
                digest = hashlib.sha256(f"{run_id}:{supplier_id}".encode()).hexdigest()[:12].upper()
                po_id = f"PO-{digest}"
                payment_id = f"PAY-{digest}"
                amount = group["amount_cents"] / 100
                delivery = date.today() + timedelta(days=group["lead_time_days"])
                con.execute("""INSERT INTO purchase_orders
                    (po_id, business_id, supplier_id, total_amount, order_date,
                     expected_delivery, status, interception_reason)
                    VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?)
                    ON CONFLICT (po_id) DO NOTHING""", [
                    po_id, state["business_id"], supplier_id, amount, date.today(), delivery,
                    f"LangGraph recommendation {run_id}; pending owner approval",
                ])
                con.execute("""INSERT INTO payment_records
                    (payment_id, po_id, supplier_id, amount, idempotency_key, payment_status)
                    VALUES (?, ?, ?, ?, ?, 'AWAITING_APPROVAL')
                    ON CONFLICT (payment_id) DO NOTHING""", [
                    payment_id, po_id, supplier_id, amount, f"{run_id}:{supplier_id}",
                ])
                purchase_orders.append({
                    "po_id": po_id, "payment_id": payment_id, "supplier_id": supplier_id,
                    "supplier_name": group["supplier_name"], "amount": f"{amount:.2f}",
                    "expected_delivery": delivery.isoformat(), "lines": group["lines"],
                    "status": "AWAITING_APPROVAL",
                })
        return {"purchase_orders": purchase_orders, "phase": "AWAITING_APPROVAL"}

    @staticmethod
    def _human_approval(state: ProcurementState) -> dict[str, Any]:
        decision = interrupt({
            "type": "procurement_approval_required",
            "run_id": state["run_id"],
            "business_id": state["business_id"],
            "explanation": state.get("explanation"),
            "recommendation": state.get("optimizer_result"),
            "purchase_orders": state.get("purchase_orders", []),
        })
        choice = str(decision.get("decision", "")).lower()
        if choice not in {"approved", "rejected"}:
            raise ValueError("Approval resume payload must be approved or rejected")
        return {
            "approval_decision": choice,
            "approved_by": decision.get("actor", "business_owner"),
            "approval_at": decision.get("decided_at"),
            "phase": "APPROVED" if choice == "approved" else "REJECTED",
        }

    @staticmethod
    def _approval_route(state: ProcurementState) -> str:
        return "approved" if state.get("approval_decision") == "approved" else "rejected"

    @staticmethod
    def _record_rejection(state: ProcurementState) -> dict[str, Any]:
        for purchase in state.get("purchase_orders", []):
            try:
                paypal_orders.reject_purchase(purchase["po_id"])
            except paypal_orders.PayPalOrderError as exc:
                logger.warning("Could not mark rejected purchase %s: %s", purchase["po_id"], exc)
        return {"phase": "REJECTED"}

    def _create_checkout(self, state: ProcurementState) -> dict[str, Any]:
        results = []
        for po in state.get("purchase_orders", []):
            try:
                checkout = paypal_orders.approve_purchase(
                    po["po_id"],
                    return_url=self._callback_url("success", state["run_id"], po["po_id"]),
                    cancel_url=self._callback_url("cancelled", state["run_id"], po["po_id"]),
                )
                if not checkout.get("approval_url"):
                    checkout["status"] = "CHECKOUT_FAILED"
                    checkout["error"] = "PayPal did not return an approval link. Retry checkout or inspect the transaction status."
                results.append({**checkout, "supplier_name": po["supplier_name"],
                                "error": checkout.get("error")})
            except paypal_orders.PayPalOrderError as exc:
                logger.warning("PayPal checkout creation failed for %s: %s", po["po_id"], exc)
                results.append({"po_id": po["po_id"], "supplier_name": po["supplier_name"],
                                "status": "FAILED", "error": str(exc)})
        succeeded = all(item.get("approval_url") for item in results) if results else False
        return {"checkout_results": results,
                "phase": "CHECKOUT_PENDING" if succeeded else "APPROVED_CHECKOUT_FAILED"}

    @staticmethod
    def _callback_url(outcome: str, run_id: str, po_id: str) -> str:
        default = "http://localhost:8501/"
        key = "PAYPAL_RETURN_URL" if outcome == "success" else "PAYPAL_CANCEL_URL"
        base = os.getenv(key, default)
        parts = urlsplit(base)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query.update({"checkout": outcome, "run_id": run_id, "po_id": po_id})
        return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", urlencode(query), parts.fragment))

    @staticmethod
    def _finish_without_purchase(state: ProcurementState) -> dict[str, Any]:
        result = state.get("optimizer_result", {})
        if not result.get("recommendation_valid"):
            phase = "INFEASIBLE" if result.get("status") == "INFEASIBLE" else "INVALID_INPUT"
        else:
            phase = "NO_PURCHASE_RECOMMENDED"
        return {"phase": phase}

