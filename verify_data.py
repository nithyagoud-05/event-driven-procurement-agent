
from pathlib import Path
import duckdb

DB_PATH = Path(__file__).parent / "database" / "procurement.duckdb"

con = duckdb.connect(str(DB_PATH), read_only=True)

tables = [
    "business_accounts",
    "cash_ledger",
    "receivables",
    "mandatory_obligations",
    "inventory",
    "suppliers",
    "supplier_quotes",
    "purchase_orders",
    "procurement_events",
    "payment_records",
    "audit_logs"
]

print("DATABASE VERIFICATION")
print("-" * 35)

for table in tables:
    count = con.execute(
        f"SELECT COUNT(*) FROM {table}"
    ).fetchone()[0]

    print(f"{table}: {count} records")

print("\nCASH POSITION")

cash = con.execute("""
    SELECT COALESCE(SUM(amount), 0)
    FROM cash_ledger
    WHERE business_id = 'BUS001'
      AND status = 'COMPLETED'
""").fetchone()[0]

payroll = con.execute("""
    SELECT COALESCE(SUM(amount), 0)
    FROM mandatory_obligations
    WHERE business_id = 'BUS001'
      AND status = 'PENDING'
""").fetchone()[0]

print(f"Opening cash: ${cash}")
print(f"Pending payroll: ${payroll}")
print(f"Cash after reserving payroll: ${cash - payroll}")

con.close()
