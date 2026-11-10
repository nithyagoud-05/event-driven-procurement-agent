"""Deterministic constraint tests for the standalone CP-SAT optimizer."""
import unittest
from decimal import Decimal

from procurement_optimizer import (
    InventoryItem,
    OptimizationConfig,
    ProcurementInput,
    SupplierQuote,
    optimize_business,
    optimize_procurement,
)


def item(*, stock=0, demand=10, safety=0, product_id="P1"):
    return InventoryItem(product_id, f"Product {product_id}", stock, demand, safety)


def quote(*, price="5.00", supplier="S1", name="Supplier 1", qty=100,
          moq=1, lead=2, product_id="P1", quote_id="Q1"):
    return SupplierQuote(quote_id, supplier, name, product_id, Decimal(price), moq, qty, lead)


def request(*, cash="1000.00", payroll="0.00", items=None, quotes=None,
            config=None, delayed="0.00"):
    return ProcurementInput(
        available_cash=Decimal(cash),
        reserved_payroll=Decimal(payroll),
        inventory=items if items is not None else [item()],
        quotes=quotes if quotes is not None else [quote()],
        config=config or OptimizationConfig(),
        delayed_receivables=Decimal(delayed),
    )


class ProcurementOptimizerTests(unittest.TestCase):
    def test_sufficient_liquidity_fulfills_urgent_inventory(self):
        result = optimize_procurement(request(
            cash="500.00", payroll="100.00",
            items=[item(stock=0, demand=10, safety=5)],
            quotes=[quote(price="10.00", qty=20)],
        ))
        self.assertIn(result["status"], {"OPTIMAL", "FEASIBLE"})
        self.assertTrue(result["recommendation_valid"])
        self.assertEqual(result["fulfilment_summary"][0]["fulfilled_quantity"], 15)
        self.assertEqual(result["total_procurement_cost"], "150.00")

    def test_delayed_receivable_does_not_expand_current_budget(self):
        result = optimize_procurement(request(
            cash="200.00", payroll="100.00", delayed="5000.00",
            items=[item(demand=4)], quotes=[quote(price="100.00", qty=10)],
            config=OptimizationConfig(minimum_fill_ratio=1.0),
        ))
        self.assertEqual(result["status"], "INFEASIBLE")
        self.assertFalse(result["recommendation_valid"])
        self.assertEqual(result["delayed_receivables_excluded"], "5000.00")
        self.assertEqual(result["total_procurement_cost_cents"], 0)
        self.assertEqual(result["procurement_budget_cents"], 10000)

    def test_payroll_reserve_is_never_spent(self):
        result = optimize_procurement(request(
            cash="500.00", payroll="450.00", items=[item(demand=8)],
            quotes=[quote(price="10.00", qty=20)],
        ))
        self.assertLessEqual(result["total_procurement_cost_cents"], 5000)
        self.assertEqual(result["free_cash_after_payroll_and_purchase"], "0.00")
        self.assertEqual(result["reserved_payroll"], "450.00")

    def test_multiple_suppliers_respect_cost_and_lead_time_tradeoff(self):
        result = optimize_procurement(request(
            items=[item(demand=5)],
            quotes=[
                quote(price="5.00", supplier="SLOW", name="Slow cheap", qty=10,
                      lead=10, quote_id="Q-SLOW"),
                quote(price="7.00", supplier="FAST", name="Fast local", qty=10,
                      lead=1, quote_id="Q-FAST"),
            ],
            config=OptimizationConfig(lead_time_weight=1000),
        ))
        self.assertEqual({q["supplier_id"] for q in result["recommended_quantities"]}, {"FAST"})
        self.assertEqual(result["recommended_quantities"][0]["lead_time_days"], 1)

    def test_moq_is_enforced_and_can_create_overbuy(self):
        result = optimize_procurement(request(
            cash="100.00", items=[item(demand=5)],
            quotes=[quote(price="1.00", qty=10, moq=8)],
        ))
        self.assertEqual(result["recommended_quantities"][0]["quantity"], 8)
        self.assertGreaterEqual(result["recommended_quantities"][0]["quantity"], 8)

    def test_inventory_already_sufficient_produces_no_purchase(self):
        result = optimize_procurement(request(
            cash="100.00", items=[item(stock=50, demand=10, safety=10)],
        ))
        self.assertEqual(result["recommended_quantities"], [])
        self.assertEqual(result["total_procurement_cost_cents"], 0)
        self.assertTrue(result["recommendation_valid"])

    def test_infeasible_minimum_fill_is_not_a_valid_recommendation(self):
        result = optimize_procurement(request(
            cash="20.00", items=[item(demand=10)],
            quotes=[quote(price="5.00", qty=10, moq=5)],
            config=OptimizationConfig(minimum_fill_ratio=1.0),
        ))
        self.assertEqual(result["status"], "INFEASIBLE")
        self.assertFalse(result["recommendation_valid"])
        self.assertTrue(result["infeasibility_reason"])
        self.assertEqual(result["recommended_quantities"], [])

    def test_missing_supplier_data_is_invalid(self):
        result = optimize_procurement(request(quotes=[quote(name="")]))
        self.assertEqual(result["status"], "INVALID_INPUT")
        self.assertFalse(result["recommendation_valid"])

    def test_quantities_are_unique_nonnegative_integers(self):
        result = optimize_procurement(request(
            cash="100.00", items=[item(demand=4)],
            quotes=[quote(qty=10, moq=2)],
        ))
        lines = result["recommended_quantities"]
        self.assertEqual(len({line["quote_id"] for line in lines}), len(lines))
        self.assertTrue(all(isinstance(line["quantity"], int) and line["quantity"] > 0 for line in lines))

    def test_database_loader_reads_seed_cash_and_payroll_without_delayed_cash(self):
        result = optimize_business("BUS001")
        self.assertEqual(result["available_cash"], "5000.00")
        self.assertEqual(result["reserved_payroll"], "3500.00")
        self.assertEqual(result["delayed_receivables_excluded"], "3000.00")
        self.assertEqual(result["procurement_budget"], "1500.00")


if __name__ == "__main__":
    unittest.main()
