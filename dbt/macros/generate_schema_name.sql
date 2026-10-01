{#
  Override dbt's default schema naming.

  Default behavior: `<target.schema>_<custom_schema>` (e.g. main_staging).
  This macro makes the custom schema the schema directly (e.g. staging),
  which is cleaner for a portfolio project and matches common dbt style.

  See: https://docs.getdbt.com/docs/build/custom-schemas
#}

{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
