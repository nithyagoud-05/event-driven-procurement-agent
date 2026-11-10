
from pathlib import Path
import duckdb

# Project paths
BASE_DIR = Path(__file__).resolve().parent
DATABASE_DIR = BASE_DIR / "database"
DATA_DIR = BASE_DIR / "data"

DB_PATH = DATABASE_DIR / "procurement.duckdb"
SCHEMA_PATH = DATABASE_DIR / "schema.sql"

def initialize_database():
    DATABASE_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)

    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"Schema file not found: {SCHEMA_PATH}")

    schema = SCHEMA_PATH.read_text(encoding="utf-8")

    connection = duckdb.connect(str(DB_PATH))

    try:
        connection.execute(schema)
        print("Database initialized successfully!")

        tables = connection.execute(
            "SHOW TABLES"
        ).fetchall()

        print("\nTables created:")
        for table in tables:
            print(f"- {table[0]}")

        print(f"\nTotal tables: {len(tables)}")
        print(f"Database location: {DB_PATH}")

    finally:
        connection.close()


if __name__ == "__main__":
    initialize_database()
