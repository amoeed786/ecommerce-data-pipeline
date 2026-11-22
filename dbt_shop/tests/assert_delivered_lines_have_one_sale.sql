-- Money reconciliation: every delivered (or later returned) order line is credited to its seller exactly once.
-- Returns order lines that are missing a sale entry or have more than one.
with sales as (
    select order_item_id, count(*) as n, sum(gross_sales) as credited
    from {{ ref('fct_ledger') }} where entry_type = 'sale' group by order_item_id
)
select i.order_item_id, i.net_amount, s.n, s.credited
from {{ ref('fct_order_items') }} i
join {{ ref('stg_shipments') }} sh on sh.shipment_id = i.shipment_id
left join sales s using (order_item_id)
where sh.status in ('delivered', 'returned')
  and (s.n is null or s.n <> 1 or abs(s.credited - i.net_amount) > 0.01)
