
CREATE TABLE IF NOT EXISTS business_accounts (
    business_id VARCHAR PRIMARY KEY,
    business_name VARCHAR NOT NULL,
    currency VARCHAR DEFAULT 'USD',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cash_ledger (
    transaction_id VARCHAR PRIMARY KEY,
    business_id VARCHAR NOT NULL,
    transaction_type VARCHAR NOT NULL,
    amount DECIMAL(12, 2) NOT NULL,
    description VARCHAR,
    transaction_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status VARCHAR DEFAULT 'COMPLETED'
);

CREATE TABLE IF NOT EXISTS receivables (
    receivable_id VARCHAR PRIMARY KEY,
    business_id VARCHAR NOT NULL,
    customer_name VARCHAR,
    expected_amount DECIMAL(12, 2) NOT NULL,
    expected_date DATE NOT NULL,
    actual_date DATE,
    status VARCHAR DEFAULT 'PENDING'
);

CREATE TABLE IF NOT EXISTS mandatory_obligations (
    obligation_id VARCHAR PRIMARY KEY,
    business_id VARCHAR NOT NULL,
    obligation_type VARCHAR NOT NULL,
    amount DECIMAL(12, 2) NOT NULL,
    due_date DATE NOT NULL,
    priority INTEGER DEFAULT 1,
    status VARCHAR DEFAULT 'PENDING'
);

CREATE TABLE IF NOT EXISTS inventory (
    product_id VARCHAR PRIMARY KEY,
    business_id VARCHAR NOT NULL,
    product_name VARCHAR NOT NULL,
    current_stock INTEGER DEFAULT 0,
    daily_demand INTEGER DEFAULT 0,
    safety_stock INTEGER DEFAULT 0,
    unit_cost DECIMAL(12, 2),
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS suppliers (
    supplier_id VARCHAR PRIMARY KEY,
    supplier_name VARCHAR NOT NULL,
    receiver_email VARCHAR,
    is_active BOOLEAN DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS supplier_quotes (
    quote_id VARCHAR PRIMARY KEY,
    supplier_id VARCHAR NOT NULL,
    product_id VARCHAR NOT NULL,
    unit_price DECIMAL(12, 2) NOT NULL,
    minimum_order_quantity INTEGER DEFAULT 1,
    available_quantity INTEGER NOT NULL,
    lead_time_days INTEGER NOT NULL,
    quote_valid_until DATE
);

CREATE TABLE IF NOT EXISTS purchase_orders (
    po_id VARCHAR PRIMARY KEY,
    business_id VARCHAR NOT NULL,
    supplier_id VARCHAR,
    total_amount DECIMAL(12, 2) NOT NULL,
    order_date DATE NOT NULL,
    expected_delivery DATE,
    status VARCHAR DEFAULT 'PENDING',
    interception_reason VARCHAR
);

CREATE TABLE IF NOT EXISTS procurement_events (
    event_id VARCHAR PRIMARY KEY,
    business_id VARCHAR NOT NULL,
    event_type VARCHAR NOT NULL,
    related_receivable_id VARCHAR,
    event_payload JSON,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    processed BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS payment_records (
    payment_id VARCHAR PRIMARY KEY,
    po_id VARCHAR NOT NULL,
    supplier_id VARCHAR NOT NULL,
    amount DECIMAL(12, 2) NOT NULL,
    paypal_batch_id VARCHAR,
    idempotency_key VARCHAR UNIQUE NOT NULL,
    payment_status VARCHAR DEFAULT 'AWAITING_APPROVAL',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_logs (
    audit_id VARCHAR PRIMARY KEY,
    event_id VARCHAR NOT NULL,
    action VARCHAR NOT NULL,
    details VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
