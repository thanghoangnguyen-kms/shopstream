{% macro generate_schema_name(custom_schema_name, node) -%}
    {{ custom_schema_name if custom_schema_name is not none else target.schema }}
{%- endmacro %}

{#- Overrides dbt-duckdb's macro to add the format-version clause dbt 2.0.6 ignores. Keeps the
    full dbt-duckdb 1.11.0 signature so the same file works on the dbt-core fallback. -#}
{% macro duckdb__create_table_as(
    temporary,
    relation,
    compiled_code,
    language='sql',
    partitioned_by=none,
    sorted_by=none
) -%}
    {%- set fv = config.get('iceberg_version', none) -%}
    {%- set cat = config.get('catalog_name', none) -%}
    {%- set versioned = fv and cat and not temporary and relation.database == cat -%}
    create {% if temporary %}temporary {% endif %}table
    {{ relation.include(database=(not temporary), schema=(not temporary)) }}
    {% if versioned %}with ('format-version' = {{ fv }}){% endif %} as (
        {{ compiled_code }}
    );
{%- endmacro %}

{#- DuckDB's Iceberg MERGE allows one UPDATE or DELETE action per statement, so this returns two
    statements in one string. -#}
{% macro get_incremental_iceberg_merge_sql(arg_dict) -%}
    {%- set target = arg_dict['target_relation'] -%}
    {%- set source = arg_dict['temp_relation'] -%}
    {%- set key = arg_dict['unique_key'] -%}
    merge into {{ target }} as d using {{ source }} as s on (s.{{ key }} = d.{{ key }})
    when matched and s.is_deleted then delete;
    merge into {{ target }} as d
    using (select * from {{ source }} where not is_deleted) as s
    on (s.{{ key }} = d.{{ key }})
    when matched then update by name
    when not matched then insert by name
{%- endmacro %}
