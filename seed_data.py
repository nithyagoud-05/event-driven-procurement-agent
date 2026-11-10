
from pathlib import Path
import duckdb

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "database" / "procurement.duckdb"

def seed_database():
    con = duckdb.connect(str(DB_PATH))

    try:
        # Clear previous demo data so this script can be rerun safely.
        tables = [
            "audit_logs",
            "payment_records",
            "procurement_events",
            "purchase_orders",
            "supplier_quotes",
            "inventory",
            "mandatory_obligations",
            "receivables",
            "cash_ledger",
            "suppliers",
            "business_accounts"
        ]

        for table in tables:
            con.execute(f"DELETE FROM {table}")

        # 1. Business account
        con.execute("""
            INSERT INTO business_accounts
            (business_id, business_name, currency)
            VALUES ('BUS001', 'FreshMart Grocery', 'USD')
        """)

        # 2. Cash ledger: initial available cash
        con.execute("""
            INSERT INTO cash_ledger
            (transaction_id, business_id, transaction_type,
             amount, description, status)
            VALUES
            ('TXN001', 'BUS001', 'CASH_IN',
             5000.00, 'Opening business cash balance', 'COMPLETED')
        """)

        # 3. Expected customer payment: currently delayed
        con.execute("""
            INSERT INTO receivables
            (receivable_id, business_id, customer_name,
             expected_amount, expected_date, status)
            VALUES
            ('REC001', 'BUS001', 'City Retail Distributor',
             3000.00, '2026-11-09', 'DELAYED')
        """)

        # 4. Mandatory payroll obligation
        con.execute("""
            INSERT INTO mandatory_obligations
            (obligation_id, business_id, obligation_type,
             amount, due_date, priority, status)
            VALUES
            ('OBL001', 'BUS001', 'PAYROLL',
             3500.00, '2026-11-13', 1, 'PENDING')
        """)

        # 5. Inventory
        con.execute("""
            INSERT INTO inventory
            (product_id, business_id, product_name,
             current_stock, daily_demand, safety_stock, unit_cost)
            VALUES
            ('PROD001', 'BUS001', 'Rice Bags',
             10, 8, 20, 25.00),

            ('PROD002', 'BUS001', 'Cooking Oil',
             12, 6, 18, 10.00),

            ('PROD003', 'BUS001', 'Wheat Flour',
             15, 5, 15, 8.00)
        """)

        # 6. Suppliers
        con.execute("""
            INSERT INTO suppliers
            (supplier_id, supplier_name, receiver_email, is_active)
            VALUES
            ('SUP001', 'Prime Wholesale', NULL, TRUE),
            ('SUP002', 'QuickSupply Local', NULL, TRUE)
        """)

        # 7. Supplier quotes
        con.execute("""
            INSERT INTO supplier_quotes
            (quote_id, supplier_id, product_id, unit_price,
             minimum_order_quantity, available_quantity,
             lead_time_days, quote_valid_until)
            VALUES
            ('Q001', 'SUP001', 'PROD001', 23.00, 50, 100, 4, '2026-11-20'),
            ('Q002', 'SUP002', 'PROD001', 25.00, 10, 40, 1, '2026-11-20'),
            ('Q003', 'SUP001', 'PROD002', 9.00, 30, 80, 4, '2026-11-20'),
            ('Q004', 'SUP002', 'PROD002', 10.00, 10, 30, 1, '2026-11-20'),
            ('Q005', 'SUP001', 'PROD003', 7.00, 25, 60, 4, '2026-11-20'),
            ('Q006', 'SUP002', 'PROD003', 8.00, 10, 25, 1, '2026-11-20')
        """)

        # 8. Pending bulk purchase order
        con.execute("""
            INSERT INTO purchase_orders
            (po_id, business_id, supplier_id, total_amount,
             order_date, expected_delivery, status, interception_reason)
            VALUES
            ('PO001', 'BUS001', 'SUP001', 2000.00,
             '2026-11-08', '2026-11-12', 'PENDING',
             'Awaiting review after delayed receivable')
        """)

        # 9. Procurement event
        con.execute("""
            INSERT INTO procurement_events
            (event_id, business_id, event_type,
             related_receivable_id, event_payload, processed)
            VALUES
            ('EVT001', 'BUS001', 'PAYMENT_DELAYED',
             'REC001',
             '{"amount": 3000, "source": "DEMO_SIMULATION"}',
             FALSE)
        """)

        # 10. Payment record: approval required, no payout executed
        con.execute("""
            INSERT INTO payment_records
            (payment_id, po_id, supplier_id, amount,
             paypal_batch_id, idempotency_key, payment_status)
            VALUES
            ('PAY001', 'PO001', 'SUP001', 2000.00,
             NULL, 'PO001-PAYMENT-V1', 'AWAITING_APPROVAL')
        """)

        # 11. Audit log
        con.execute("""
            INSERT INTO audit_logs
            (audit_id, event_id, action, details)
            VALUES
            ('AUD001', 'EVT001', 'DEMO_EVENT_CREATED',
             'Simulated delayed customer payment for FreshMart')
        """)

        print("Mock data inserted successfully!")

        print("\nBusiness:")
        print(con.execute(
            "SELECT * FROM business_accounts"
        ).fetchdf())

        print("\nCash ledger:")
        print(con.execute(
            "SELECT * FROM cash_ledger"
        ).fetchdf())

        print("\nReceivables:")
        print(con.execute(
            "SELECT * FROM receivables"
        ).fetchdf())

        print("\nInventory:")
        print(con.execute(
            "SELECT * FROM inventory"
        ).fetchdf())

        print("\nPurchase orders:")
        print(con.execute(
            "SELECT * FROM purchase_orders"
        ).fetchdf())

    finally:
        con.close()


if __name__ == "__main__":
    seed_database()
