{{ config(
    materialized='incremental',
    iceberg_catalog=var('catalog', none),
    schema='silver_spike',
    iceberg_version=3,
    incremental_strategy='iceberg_merge',
    unique_key='id'
) }}

select
    id,
    name,
    is_deleted
from {{ ref('changes') }}
where batch = {{ var('batch', 1) }}
