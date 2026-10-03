{{ config(
    materialized='incremental',
    iceberg_catalog=var('catalog', none),
    schema=var('gold_schema', 'gold'),
    iceberg_version=3,
    tags=['gold']
) }}

select
    customer_id,
    segment,
    '{{ var('publish_id', 'A') }}' as publish_id
from {{ ref('customers') }}
