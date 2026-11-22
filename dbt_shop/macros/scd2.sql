{#-
  SCD Type 2 from a version history.

  Input: one row per captured version (key + tracked columns + updated_at).
  Output: one row per *real change*, with a validity window:
      valid_from   when this version became true (first version: 1900-01-01, so any older fact still finds it)
      valid_to     when the next version replaced it (current version: 9999-12-31)
      is_current   true for the latest version
  Versions where none of the tracked columns changed (e.g. only updated_at moved) are collapsed.

  Facts join with:  fact_ts >= valid_from AND fact_ts < valid_to
-#}
{% macro scd2(relation, key, tracked) %}
with versions as (
    select
        *,
        md5(concat_ws('||', {% for c in tracked %}coalesce(cast({{ c }} as varchar), '∅'){{ ", " if not loop.last }}{% endfor %})) as _row_hash
    from {{ relation }}
),
changes as (
    select
        *,
        lag(_row_hash) over (partition by {{ key }} order by updated_at) as _prev_hash
    from versions
),
kept as (
    select * exclude (_prev_hash)
    from changes
    where _prev_hash is null or _prev_hash <> _row_hash
)
select
    md5(cast({{ key }} as varchar) || '@' || cast(updated_at as varchar)) as {{ key | replace('_id', '_sk') }},
    * exclude (_row_hash),
    case when row_number() over (partition by {{ key }} order by updated_at) = 1
         then timestamp '1900-01-01' else updated_at end                         as valid_from,
    coalesce(lead(updated_at) over (partition by {{ key }} order by updated_at),
             timestamp '9999-12-31')                                              as valid_to,
    lead(updated_at) over (partition by {{ key }} order by updated_at) is null   as is_current,
    row_number() over (partition by {{ key }} order by updated_at)              as version
from kept
{% endmacro %}
