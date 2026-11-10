"""Liquidity-aware procurement optimization using Google OR-Tools CP-SAT.

The solver is independent of the UI and payment execution. Amounts cross the
solver boundary as integer cents; the small data classes are also convenient
for deterministic offline scenarios and future API integration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from math import ceil
from pathlib import Path
from typing import Any, Iterable

import duckdb
from ortools.sat.python import cp_model


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "database" / "procurement.duckdb"


@dataclass(frozen=True)
class InventoryItem:
    product_id: str
    product_name: str
    current_stock: int
    daily_demand: int
    safety_stock: int


@dataclass(frozen=True)
class SupplierQuote:
    quote_id: str
    supplier_id: str
    supplier_name: str
    product_id: str
    unit_price: Decimal | str | float
    minimum_order_quantity: int
    available_quantity: int
    lead_time_days: int
    quote_valid_until: date | str | None = None


@dataclass(frozen=True)
class OptimizationConfig:
    """Policy knobs; monetary values are dollars at the public boundary."""

    min_cash_buffer: Decimal | str | float = Decimal("0.00")
    procurement_budget: Decimal | str | float | None = None
    planning_horizon_days: int = 1
    minimum_fill_ratio: float = 0.0
    urgency_weight: int = 100_000
    cost_weight: int = 1
    lead_time_weight: int = 10
    max_solve_seconds: float = 5.0


@dataclass(frozen=True)
class ProcurementInput:
    available_cash: Decimal | str | float
    reserved_payroll: Decimal | str | float
    inventory: Iterable[InventoryItem]
    quotes: Iterable[SupplierQuote]
    config: OptimizationConfig = field(default_factory=OptimizationConfig)
    delayed_receivables: Decimal | str | float = Decimal("0.00")


def _cents(value: Decimal | str | float | int, field_name: str) -> int:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a valid monetary amount") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"{field_name} must have no more than two decimal places")
    return int(cents)


def _dollars(cents: int) -> str:
    return f"{Decimal(cents) / 100:.2f}"


def _invalid(message: str, available_cash_cents: int = 0,
             reserved_payroll_cents: int = 0) -> dict[str, Any]:
    return {
        "status": "INVALID_INPUT",
        "recommendation_valid": False,
        "selected_suppliers": [],
        "recommended_quantities": [],
        "total_procurement_cost": "0.00",
        "total_procurement_cost_cents": 0,
        "remaining_available_cash": _dollars(available_cash_cents),
        "remaining_available_cash_cents": available_cash_cents,
        "free_cash_after_payroll_and_purchase": _dollars(
            max(0, available_cash_cents - reserved_payroll_cents)),
        "reserved_payroll": _dollars(reserved_payroll_cents),
        "fulfilment_summary": [],
        "explanation": message,
        "infeasibility_reason": None,
        "data_issues": [message],
    }


def _validate(data: ProcurementInput) -> tuple[int, int, int, list[InventoryItem], list[SupplierQuote], list[str]]:
    cash = _cents(data.available_cash, "available_cash")
    payroll = _cents(data.reserved_payroll, "reserved_payroll")
    _cents(data.delayed_receivables, "delayed_receivables")
    buffer = _cents(data.config.min_cash_buffer, "min_cash_buffer")
    if data.config.procurement_budget is None:
        policy_budget = None
    else:
        policy_budget = _cents(data.config.procurement_budget, "procurement_budget")
    if not isinstance(data.config.planning_horizon_days, int) or data.config.planning_horizon_days < 0:
        raise ValueError("planning_horizon_days must be a non-negative integer")
    if not 0 <= data.config.minimum_fill_ratio <= 1:
        raise ValueError("minimum_fill_ratio must be between 0 and 1")
    if data.config.max_solve_seconds <= 0:
        raise ValueError("max_solve_seconds must be positive")
    if min(data.config.urgency_weight, data.config.cost_weight, data.config.lead_time_weight) < 0:
        raise ValueError("optimization weights must be non-negative integers")

    inventory = list(data.inventory)
    quotes = list(data.quotes)
    products: set[str] = set()
    for item in inventory:
        if not item.product_id or not item.product_name:
            raise ValueError("inventory product ID and name are required")
        if item.product_id in products:
            raise ValueError(f"duplicate inventory product ID: {item.product_id}")
        products.add(item.product_id)
        for name in ("current_stock", "daily_demand", "safety_stock"):
            value = getattr(item, name)
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} for {item.product_id} must be a non-negative integer")

    quote_ids: set[str] = set()
    issues: list[str] = []
    for quote in quotes:
        if not quote.quote_id or quote.quote_id in quote_ids:
            raise ValueError("supplier quote IDs are required and must be unique")
        quote_ids.add(quote.quote_id)
        if quote.product_id not in products:
            raise ValueError(f"quote {quote.quote_id} references unknown product {quote.product_id}")
        if not quote.supplier_id or not quote.supplier_name:
            raise ValueError(f"quote {quote.quote_id} is missing supplier data")
        price = _cents(quote.unit_price, f"unit_price for quote {quote.quote_id}")
        if price <= 0:
            raise ValueError(f"unit price for quote {quote.quote_id} must be positive")
        if not isinstance(quote.minimum_order_quantity, int) or quote.minimum_order_quantity <= 0:
            raise ValueError(f"MOQ for quote {quote.quote_id} must be a positive integer")
        if not isinstance(quote.available_quantity, int) or quote.available_quantity < quote.minimum_order_quantity:
            issues.append(f"quote {quote.quote_id} has no usable quantity (availability is below MOQ)")
        if not isinstance(quote.lead_time_days, int) or quote.lead_time_days < 0:
            raise ValueError(f"lead time for quote {quote.quote_id} must be a non-negative integer")
        if quote.quote_valid_until:
            valid_until = quote.quote_valid_until
            if isinstance(valid_until, str):
                try:
                    valid_until = date.fromisoformat(valid_until)
                except ValueError as exc:
                    raise ValueError(f"quote {quote.quote_id} has invalid quote_valid_until") from exc
            if valid_until < date.today():
                issues.append(f"quote {quote.quote_id} is expired")
    return cash, payroll, min(max(0, cash - payroll - buffer), policy_budget) if policy_budget is not None else max(0, cash - payroll - buffer), inventory, quotes, issues


def optimize_procurement(data: ProcurementInput) -> dict[str, Any]:
    """Return a CP-SAT procurement plan, never a recommendation on unknown status."""
    try:
        cash, payroll, budget, inventory, quotes, issues = _validate(data)
    except (TypeError, ValueError) as exc:
        try:
            cash = _cents(data.available_cash, "available_cash")
            payroll = _cents(data.reserved_payroll, "reserved_payroll")
        except (TypeError, ValueError):
            cash, payroll = 0, 0
        return _invalid(str(exc), cash, payroll)

    if payroll > cash:
        issues.append("reserved payroll exceeds current cash; procurement budget is zero")
    if data.delayed_receivables and _cents(data.delayed_receivables, "delayed_receivables") > 0:
        issues.append("delayed receivables are excluded from spendable cash")
    delayed_cents = _cents(data.delayed_receivables, "delayed_receivables")

    demand: dict[str, int] = {}
    urgency: dict[str, int] = {}
    for item in inventory:
        needed = max(0, item.daily_demand * data.config.planning_horizon_days
                     + item.safety_stock - item.current_stock)
        demand[item.product_id] = needed
        # A low days-of-cover item gets a larger fulfilment reward.
        urgency[item.product_id] = (max(1, min(100, (100 * item.daily_demand)
                                               // (item.daily_demand + item.current_stock)))
                                    if item.daily_demand else 1)

    model = cp_model.CpModel()
    quantity_vars: dict[str, cp_model.IntVar] = {}
    selected_vars: dict[str, cp_model.IntVar] = {}
    for quote in quotes:
        usable = max(0, quote.available_quantity)
        valid_until = quote.quote_valid_until
        if isinstance(valid_until, str):
            valid_until = date.fromisoformat(valid_until)
        if valid_until and valid_until < date.today():
            usable = 0
        selected = model.NewBoolVar(f"selected_{quote.quote_id}")
        quantity = model.NewIntVar(0, usable, f"quantity_{quote.quote_id}")
        quantity_vars[quote.quote_id] = quantity
        selected_vars[quote.quote_id] = selected
        model.Add(quantity <= usable * selected)
        model.Add(quantity >= quote.minimum_order_quantity * selected)

    product_fulfilled: dict[str, cp_model.IntVar] = {}
    for product_id, needed in demand.items():
        total = sum((quantity_vars[q.quote_id] for q in quotes if q.product_id == product_id), 0)
        fulfilled = model.NewIntVar(0, needed, f"fulfilled_{product_id}")
        model.AddMinEquality(fulfilled, [total, needed])
        product_fulfilled[product_id] = fulfilled
        if data.config.minimum_fill_ratio > 0:
            required_minimum = ceil(needed * data.config.minimum_fill_ratio)
            model.Add(fulfilled >= required_minimum)

    # Budget is current cash less payroll and buffer. Delayed receivables never enter it.
    cost_terms = []
    for quote in quotes:
        price = _cents(quote.unit_price, f"unit_price for quote {quote.quote_id}")
        cost_terms.append(price * quantity_vars[quote.quote_id])
    total_cost = sum(cost_terms, 0)
    model.Add(total_cost <= budget)

    objective_terms = []
    for product_id, fulfilled in product_fulfilled.items():
        objective_terms.append(data.config.urgency_weight * urgency[product_id] * fulfilled)
    for quote in quotes:
        unit_cost = _cents(quote.unit_price, f"unit_price for quote {quote.quote_id}")
        quantity = quantity_vars[quote.quote_id]
        objective_terms.append(-data.config.cost_weight * unit_cost * quantity)
        objective_terms.append(-data.config.lead_time_weight * quote.lead_time_days * selected_vars[quote.quote_id])
    model.Maximize(sum(objective_terms, 0))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = data.config.max_solve_seconds
    status_code = solver.Solve(model)
    status = solver.StatusName(status_code)
    if status_code not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        reason = (
            "Configured minimum fulfilment cannot be met within the cash available after payroll and cash buffer, "
            "or supplier MOQ/availability constraints."
            if status_code == cp_model.INFEASIBLE else
            "CP-SAT did not find a feasible solution within the configured solve limit."
        )
        return {
            "status": status,
            "recommendation_valid": False,
            "selected_suppliers": [],
            "recommended_quantities": [],
            "total_procurement_cost": "0.00",
            "total_procurement_cost_cents": 0,
            "available_cash": _dollars(cash),
            "available_cash_cents": cash,
            "remaining_available_cash": _dollars(cash),
            "remaining_available_cash_cents": cash,
            "free_cash_after_payroll_and_purchase": _dollars(budget),
            "reserved_payroll": _dollars(payroll),
            "reserved_payroll_cents": payroll,
            "procurement_budget": _dollars(budget),
            "procurement_budget_cents": budget,
            "minimum_cash_buffer": _dollars(_cents(data.config.min_cash_buffer, "min_cash_buffer")),
            "delayed_receivables_excluded": _dollars(delayed_cents),
            "fulfilment_summary": [],
            "explanation": reason,
            "infeasibility_reason": reason if status_code == cp_model.INFEASIBLE else None,
            "data_issues": issues,
        }

    selected_quotes = []
    for quote in quotes:
        qty = solver.Value(quantity_vars[quote.quote_id])
        if qty <= 0:
            continue
        price_cents = _cents(quote.unit_price, f"unit_price for quote {quote.quote_id}")
        selected_quotes.append({
            "quote_id": quote.quote_id,
            "supplier_id": quote.supplier_id,
            "supplier_name": quote.supplier_name,
            "product_id": quote.product_id,
            "quantity": qty,
            "unit_price": _dollars(price_cents),
            "unit_price_cents": price_cents,
            "line_total": _dollars(price_cents * qty),
            "line_total_cents": price_cents * qty,
            "lead_time_days": quote.lead_time_days,
        })

    spent = sum(line["line_total_cents"] for line in selected_quotes)
    summary = []
    item_names = {item.product_id: item.product_name for item in inventory}
    item_stocks = {item.product_id: item.current_stock for item in inventory}
    for product_id, needed in demand.items():
        ordered = sum(line["quantity"] for line in selected_quotes if line["product_id"] == product_id)
        fulfilled = min(needed, ordered)
        summary.append({
            "product_id": product_id,
            "product_name": item_names[product_id],
            "current_stock": item_stocks[product_id],
            "required_quantity": needed,
            "recommended_quantity": ordered,
            "fulfilled_quantity": fulfilled,
            "unfulfilled_quantity": max(0, needed - fulfilled),
            "urgency_score": urgency[product_id],
        })

    supplier_map: dict[str, dict[str, Any]] = {}
    for line in selected_quotes:
        entry = supplier_map.setdefault(line["supplier_id"], {
            "supplier_id": line["supplier_id"], "supplier_name": line["supplier_name"],
            "total_cost": "0.00", "total_cost_cents": 0, "lines": [],
        })
        entry["total_cost_cents"] += line["line_total_cents"]
        entry["lines"].append(line["quote_id"])
    for entry in supplier_map.values():
        entry["total_cost"] = _dollars(entry["total_cost_cents"])

    unmet = sum(row["unfulfilled_quantity"] for row in summary)
    if not selected_quotes:
        explanation = "No purchase is recommended: current inventory is sufficient, no usable supplier quote exists, or liquidity constraints prevent procurement."
    elif unmet:
        explanation = (f"The plan procures {sum(line['quantity'] for line in selected_quotes)} units for ${_dollars(spent)} "
                       f"while preserving payroll and the cash buffer. {unmet} units of the computed requirement remain unfilled "
                       "because of liquidity, MOQ, supplier capacity, or the configured optimization weights.")
    else:
        explanation = (f"The plan covers the computed replenishment requirements at a total cost of ${_dollars(spent)}, "
                       "within the procurement budget and while preserving payroll and the cash buffer.")
    return {
        "status": status,
        "recommendation_valid": True,
        "selected_suppliers": list(supplier_map.values()),
        "recommended_quantities": selected_quotes,
        "total_procurement_cost": _dollars(spent),
        "total_procurement_cost_cents": spent,
        "available_cash": _dollars(cash),
        "available_cash_cents": cash,
        "remaining_available_cash": _dollars(cash - spent),
        "remaining_available_cash_cents": cash - spent,
        "free_cash_after_payroll_and_purchase": _dollars(max(0, cash - payroll - spent)),
        "reserved_payroll": _dollars(payroll),
        "reserved_payroll_cents": payroll,
        "procurement_budget": _dollars(budget),
        "procurement_budget_cents": budget,
        "minimum_cash_buffer": _dollars(_cents(data.config.min_cash_buffer, "min_cash_buffer")),
        "delayed_receivables_excluded": _dollars(delayed_cents),
        "fulfilment_summary": summary,
        "explanation": explanation,
        "infeasibility_reason": None,
        "data_issues": issues,
        "objective_value": solver.ObjectiveValue(),
    }


def load_procurement_input(business_id: str, *, db_path: str | Path = DEFAULT_DB_PATH,
                           config: OptimizationConfig | None = None) -> ProcurementInput:
    """Read existing schema fields for a business; delayed receivables remain informational only."""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        cash_rows = con.execute("""SELECT transaction_type, amount FROM cash_ledger
            WHERE business_id = ? AND upper(status) = 'COMPLETED'""", [business_id]).fetchall()
        if not cash_rows:
            raise ValueError(f"No completed cash ledger records exist for {business_id}")
        cash_cents = 0
        for kind, amount in cash_rows:
            cents = _cents(abs(Decimal(str(amount))), "cash ledger amount")
            if any(word in (kind or "").upper() for word in ("OUT", "DEBIT", "EXPENSE", "PAYMENT", "WITHDRAW")):
                cash_cents -= cents
            else:
                cash_cents += cents
        payroll_value = con.execute("""SELECT COALESCE(SUM(amount), 0)
            FROM mandatory_obligations WHERE business_id = ? AND upper(status) = 'PENDING'
              AND upper(obligation_type) = 'PAYROLL'""", [business_id]).fetchone()[0]
        inventory_rows = con.execute("""SELECT product_id, product_name, current_stock,
            daily_demand, safety_stock FROM inventory WHERE business_id = ? ORDER BY product_id""",
                                     [business_id]).fetchall()
        inventory = [InventoryItem(*row) for row in inventory_rows]
        quote_rows = con.execute("""SELECT q.quote_id, q.supplier_id, s.supplier_name,
            q.product_id, q.unit_price, q.minimum_order_quantity, q.available_quantity,
            q.lead_time_days, q.quote_valid_until
            FROM supplier_quotes q
            JOIN inventory i ON q.product_id = i.product_id AND i.business_id = ?
            LEFT JOIN suppliers s ON q.supplier_id = s.supplier_id
            WHERE s.supplier_id IS NULL OR s.is_active = TRUE
            ORDER BY q.quote_id""", [business_id]).fetchall()
        quotes = [SupplierQuote(*row) for row in quote_rows]
        delayed = con.execute("""SELECT COALESCE(SUM(expected_amount), 0) FROM receivables
            WHERE business_id = ? AND upper(status) = 'DELAYED'""", [business_id]).fetchone()[0]
        return ProcurementInput(
            available_cash=Decimal(cash_cents) / 100,
            reserved_payroll=payroll_value,
            inventory=inventory,
            quotes=quotes,
            config=config or OptimizationConfig(),
            delayed_receivables=delayed,
        )
    finally:
        con.close()


def optimize_business(business_id: str, *, db_path: str | Path = DEFAULT_DB_PATH,
                      config: OptimizationConfig | None = None) -> dict[str, Any]:
    """Convenience entry point that loads the existing DuckDB data then solves."""
    try:
        data = load_procurement_input(business_id, db_path=db_path, config=config)
    except (duckdb.Error, ValueError) as exc:
        return _invalid(str(exc))
    return optimize_procurement(data)


if __name__ == "__main__":
    import json
    import sys

    business_id = sys.argv[1] if len(sys.argv) > 1 else "BUS001"
    print(json.dumps(optimize_business(business_id), indent=2))
