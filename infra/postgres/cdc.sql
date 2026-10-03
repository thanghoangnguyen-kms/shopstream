-- The Postgres side of CDC. Run by the cdc-init one-shot on every streaming start, so it is idempotent.
-- Passwords arrive only as psql variables (-v cdc_pw=...), never in the SQL text. It lives outside
-- init/ so that a clean clone and an existing volume both get it, and the init-dir tests stay as they are.
-- reviews is the stand-in table for ADR-001's seeded prompt-injection review knob; the Week 3
-- generator names the real columns.
\connect shopstream

SELECT 'CREATE ROLE cdc' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cdc') \gexec
ALTER ROLE cdc WITH LOGIN REPLICATION NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'cdc_pw';

CREATE TABLE IF NOT EXISTS reviews (
    review_id bigint PRIMARY KEY,
    product_id bigint NOT NULL,
    body text NOT NULL,
    updated_at timestamptz NOT NULL
);
ALTER TABLE reviews OWNER TO shopstream;
ALTER TABLE reviews REPLICA IDENTITY FULL;

GRANT CONNECT ON DATABASE shopstream TO cdc;
GRANT USAGE ON SCHEMA public TO cdc;
GRANT SELECT ON customers, products, orders, order_items, reviews TO cdc;

SELECT 'CREATE PUBLICATION shopstream_cdc FOR TABLE customers, products, orders, order_items, reviews' WHERE NOT EXISTS (SELECT FROM pg_publication WHERE pubname = 'shopstream_cdc') \gexec
