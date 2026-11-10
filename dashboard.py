"""Streamlit business dashboard backed by the FastAPI service."""

from __future__ import annotations

import os

import pandas as pd
import requests
import streamlit as st
from st_aggrid import AgGrid, GridOptionsBuilder


def api_base() -> str:
    value = os.getenv("API_BASE_URL", "http://localhost:8000").rstrip("/")
    if value and not value.startswith(("http://", "https://")):
        value = f"https://{value}"
    return value


def api(method: str, path: str, **kwargs):
    try:
        token = os.getenv("APPROVAL_API_TOKEN")
        if token:
            kwargs.setdefault("headers", {})["X-Approval-Token"] = token
        response = requests.request(method, f"{api_base()}{path}", timeout=45, **kwargs)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        message = str(exc)
        if getattr(exc, "response", None) is not None:
            try:
                message = exc.response.json().get("detail", message)
            except ValueError:
                pass
        st.error(f"Backend request failed: {message}")
        return None


def show_grid(rows, *, height=300):
    if not rows:
        st.info("No rows to display.")
        return
    frame = pd.DataFrame(rows)
    options = GridOptionsBuilder.from_dataframe(frame)
    options.configure_default_column(sortable=True, filter=True, resizable=True, wrapText=True,
                                     autoHeight=True)
    options.configure_pagination(paginationAutoPageSize=True)
    AgGrid(frame, gridOptions=options.build(), theme="streamlit", height=height,
           fit_columns_on_grid_load=True, allow_unsafe_jscode=False)


st.set_page_config(page_title="FreshMart | Procurement Agent", page_icon="📦", layout="wide")
st.title("FreshMart Procurement Agent")
st.caption("Cash-aware inventory recommendations with explicit owner approval and PayPal Sandbox checkout.")

query_run_id = st.query_params.get("run_id")
business_id = st.sidebar.text_input("Business ID", value=st.session_state.get("business_id", "BUS001"))
st.session_state.business_id = business_id
if query_run_id and not st.session_state.get("active_run_id"):
    st.session_state.active_run_id = query_run_id
if st.sidebar.button("Refresh dashboard"):
    st.rerun()

dashboard_data = api("GET", f"/api/dashboard/{business_id}")
if not dashboard_data:
    st.stop()

st.subheader(dashboard_data["business_name"])
cash_col, payroll_col, free_col, delay_col = st.columns(4)
cash_col.metric("Available cash", f"${dashboard_data['cash']}")
payroll_col.metric("Reserved payroll", f"${dashboard_data['reserved_payroll']}")
free_col.metric("Cash after payroll", f"${dashboard_data['cash_after_payroll']}")
delayed_total = sum(float(row["amount"]) for row in dashboard_data["delayed_receivables"] + dashboard_data["overdue_receivables"])
delay_col.metric("Delayed / overdue", f"${delayed_total:,.2f}",
                 delta=f"{len(dashboard_data['delayed_receivables']) + len(dashboard_data['overdue_receivables'])} payment(s)")

if dashboard_data["delayed_receivables"] or dashboard_data["overdue_receivables"]:
    st.warning("Incoming cash is delayed or overdue. The optimizer will exclude it from spendable cash.")

left, right = st.columns([1, 1])
with left:
    st.subheader("Inventory")
    show_grid(dashboard_data["inventory"], height=240)
with right:
    st.subheader("Incoming payments")
    show_grid(dashboard_data["delayed_receivables"] + dashboard_data["overdue_receivables"] +
              dashboard_data["expected_receivables"], height=240)

with st.expander("Supplier comparison", expanded=True):
    show_grid(dashboard_data["supplier_comparison"], height=350)

if dashboard_data["purchase_orders"]:
    with st.expander("Existing uncompleted purchase orders", expanded=True):
        show_grid(dashboard_data["purchase_orders"], height=200)
        for order in dashboard_data["purchase_orders"]:
            if order["status"].upper() == "PENDING":
                st.write(f"PO {order['po_id']} · {order.get('supplier_name') or order.get('supplier_id')} · ${order['amount']} · {order['status']}")
                if st.button("Reject this pending purchase order", key=f"reject-existing-{order['po_id']}"):
                    rejected = api("POST", f"/api/purchase-orders/{order['po_id']}/reject")
                    if rejected:
                        st.success(f"Purchase order {order['po_id']} rejected.")
                        st.rerun()

st.divider()
col, _ = st.columns([1, 3])
if col.button("Analyze liquidity and recommend procurement", type="primary", use_container_width=True):
    state = api("POST", "/api/agent/runs", json={"business_id": business_id})
    if state:
        st.session_state.active_run_id = state["run_id"]
        st.query_params["run_id"] = state["run_id"]
        st.rerun()

run_id = st.session_state.get("active_run_id")
if run_id:
    state = api("GET", f"/api/agent/runs/{run_id}")
    if state:
        st.subheader("Agent decision")
        st.caption(f"Run {run_id} · Phase: {state.get('phase', 'UNKNOWN')}")
        if state.get("risk_analysis"):
            risk = state["risk_analysis"]
            risk_message = f"Liquidity risk: {risk.get('risk_level', 'UNKNOWN')}\n\n{risk.get('analysis', '')}"
            if risk.get("risk_level") == "HIGH":
                st.error(risk_message)
            elif risk.get("risk_level") == "ELEVATED":
                st.warning(risk_message)
            else:
                st.info(risk_message)
        if state.get("optimizer_result"):
            result = state["optimizer_result"]
            status = result.get("status", "UNKNOWN")
            if result.get("recommendation_valid"):
                st.success(f"Solver: {status} · Recommended spend: ${result.get('total_procurement_cost', '0.00')} · "
                           f"Cash remaining: ${result.get('remaining_available_cash', '0.00')}")
            else:
                st.error(f"Solver: {status} · {result.get('infeasibility_reason') or result.get('explanation')}")
            st.write(state.get("explanation", result.get("explanation", "")))
            if result.get("recommended_quantities"):
                st.markdown("**Supplier and quantity recommendation**")
                show_grid(result["recommended_quantities"], height=220)
            if result.get("fulfilment_summary"):
                with st.expander("Inventory fulfilment details"):
                    show_grid(result["fulfilment_summary"], height=220)

        if state.get("waiting_for_approval"):
            st.warning("Recommendation only. No PayPal order has been created until you approve.")
            approve_col, reject_col = st.columns(2)
            if approve_col.button("Approve and create PayPal checkout", type="primary", key=f"approve-{run_id}"):
                updated = api("POST", f"/api/agent/runs/{run_id}/approval",
                              json={"decision": "approved"})
                if updated:
                    st.rerun()
            if reject_col.button("Reject recommendation", key=f"reject-{run_id}"):
                updated = api("POST", f"/api/agent/runs/{run_id}/approval",
                              json={"decision": "rejected"})
                if updated:
                    st.rerun()

        if state.get("approval_decision") == "rejected":
            st.info("Recommendation rejected. No checkout was created.")
        elif state.get("approval_decision") == "approved":
            st.subheader("PayPal checkout")
            transactions = api("GET", f"/api/agent/runs/{run_id}/transactions")
            transaction_by_po = {row["po_id"]: row for row in transactions.get("transactions", [])} if transactions else {}
            if state.get("checkout_results"):
                for checkout in state["checkout_results"]:
                    transaction = transaction_by_po.get(checkout["po_id"], {})
                    approval_url = transaction.get("approval_url") or checkout.get("approval_url")
                    transaction_status = transaction.get("status", checkout.get("status", "UNKNOWN"))
                    st.write(f"{checkout.get('supplier_name', checkout['po_id'])} · PO {checkout['po_id']} · "
                             f"Status: {transaction_status}")
                    if transaction_status == "CAPTURED":
                        st.success(f"Payment completed. Capture ID: {transaction.get('capture_id') or 'recorded'}")
                    elif approval_url:
                        label = "Continue to PayPal Sandbox" if transaction_status != "CANCELLED" else "Reopen existing PayPal order"
                        st.link_button(label, approval_url,
                                       key=f"paypal-{checkout['po_id']}")
                    elif checkout.get("error") or transaction.get("error"):
                        st.error(checkout.get("error") or transaction.get("error"))
                        if st.button("Retry PayPal order creation", key=f"retry-{checkout['po_id']}"):
                            retried = api("POST", f"/api/transactions/{checkout['po_id']}/create-order",
                                          params={"run_id": run_id})
                            if retried:
                                st.rerun()
                    if approval_url and transaction_status != "CAPTURED":
                        action_col, cancel_col = st.columns(2)
                        if transaction_status != "CANCELLED" and action_col.button("Capture after PayPal approval", key=f"capture-{checkout['po_id']}"):
                            captured = api("POST", f"/api/transactions/{checkout['po_id']}/capture",
                                           params={"run_id": run_id})
                            if captured:
                                st.success(f"PayPal capture status: {captured.get('status')}")
                                st.rerun()
                        if cancel_col.button("Cancel checkout", key=f"cancel-{checkout['po_id']}"):
                            cancelled = api("POST", f"/api/transactions/{checkout['po_id']}/cancel",
                                            params={"run_id": run_id})
                            if cancelled:
                                st.warning("Checkout cancellation recorded.")
                                st.rerun()
            if transactions and transactions.get("transactions"):
                st.markdown("**Recorded transaction status**")
                show_grid(transactions["transactions"], height=160)

st.divider()
st.caption("Recommendations, approvals, and completed PayPal captures are shown as separate workflow states.")
