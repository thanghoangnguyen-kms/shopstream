{{ config(
    materialized='incremental',
    iceberg_catalog=var('catalog', none),
    schema=var('gold_schema', 'gold'),
    iceberg_version=3,
    tags=['gold']
) }}

select
    cast(day as date) as date_day,
    '{{ var('publish_id', 'A') }}' as publish_id
from generate_series(date '2026-09-01', date '2026-09-30', interval 1 day) as spine (day)
