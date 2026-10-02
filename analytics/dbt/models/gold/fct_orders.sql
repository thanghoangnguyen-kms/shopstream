{{ config(
    materialized='incremental',
    iceberg_catalog=var('catalog', none),
    schema=var('gold_schema', 'gold'),
    iceberg_version=3,
    tags=['gold']
) }}

select
    order_id,
    customer_id,
    order_date,
    amount,
    '{{ var('publish_id', 'A') }}' as publish_id
from {{ ref('orders') }}
