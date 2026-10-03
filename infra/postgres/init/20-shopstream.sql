-- Captured tables for the CDC probes (items 5 and 11). Runs after 10-databases.sh, on first boot only.
-- The columns are a stand-in: the Week 3 generator replaces them. What the probes rely on:
--   * every table has a primary key and updated_at timestamptz NOT NULL;
--   * REPLICA IDENTITY FULL, so a delete's before image carries updated_at;
--   * no foreign keys at all, in particular none on order_items.product_id (ADR-001's
--     late-arriving-product knob).
\connect shopstream
SET ROLE shopstream;

CREATE TABLE customers (
    customer_id bigint PRIMARY KEY,
    email text NOT NULL,
    full_name text NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE TABLE products (
    product_id bigint PRIMARY KEY,
    name text NOT NULL,
    price numeric(12, 2) NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE TABLE orders (
    order_id bigint PRIMARY KEY,
    customer_id bigint NOT NULL,
    status text NOT NULL,
    currency char(3) NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE TABLE order_items (
    order_id bigint NOT NULL,
    line_no int NOT NULL,
    product_id bigint NOT NULL,
    quantity int NOT NULL,
    unit_price numeric(12, 2) NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (order_id, line_no)
);

ALTER TABLE customers REPLICA IDENTITY FULL;
ALTER TABLE products REPLICA IDENTITY FULL;
ALTER TABLE orders REPLICA IDENTITY FULL;
ALTER TABLE order_items REPLICA IDENTITY FULL;
