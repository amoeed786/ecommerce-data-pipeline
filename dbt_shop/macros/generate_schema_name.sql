{#- Use the custom schema name as-is (staging, core, analytics) instead of dbt's default
    "<target_schema>_<custom>" (main_staging, main_core, ...). Easier to read when you query. -#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {{ custom_schema_name | trim if custom_schema_name else target.schema }}
{%- endmacro %}
