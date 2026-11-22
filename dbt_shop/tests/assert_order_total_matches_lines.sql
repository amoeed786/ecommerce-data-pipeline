-- Order header total should equal the sum of its lines. Silver already WARNs about this, so here it's
-- a warning too: it reports the order (e.g. the one inject_bad_data.py creates) without failing the build.
{{ config(severity='warn') }}
select order_id, total_amount, gross_amount - discount_amount as lines_total
from {{ ref('fct_orders') }}
where abs(total_amount - (gross_amount - discount_amount)) > 0.01
