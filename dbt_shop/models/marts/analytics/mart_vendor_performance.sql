-- One row per store: sales and fulfilment quality over the last 30 days (plus all-time totals).
with ships as (
    select
        vendor_id,
        count(*) filter (where created_at >= current_date - 30)                                   as shipments_30d,
        count(*) filter (where created_at >= current_date - 30 and is_delivered)                  as delivered_30d,
        avg(case when is_cancelled then 1.0 else 0 end) filter (where created_at >= current_date - 30) as cancel_rate,
        avg(case when is_returned  then 1.0 else 0 end) filter (where created_at >= current_date - 30) as return_rate,
        avg(minutes_to_accept)        filter (where created_at >= current_date - 30)              as avg_accept_minutes,
        avg(minutes_to_accept + minutes_to_pack) filter (where created_at >= current_date - 30)   as avg_prep_minutes,
        avg(minutes_confirm_to_door)  filter (where created_at >= current_date - 30)              as avg_delivery_minutes,
        count(*)                                                                                  as shipments_all_time
    from {{ ref('fct_shipments') }}
    group by vendor_id
),
sales as (
    select
        vendor_id,
        sum(net_amount)   filter (where is_revenue and order_ts >= current_date - 30) as revenue_30d,
        sum(gross_margin) filter (where is_revenue and order_ts >= current_date - 30) as margin_30d,
        sum(quantity)     filter (where is_revenue and order_ts >= current_date - 30) as units_30d,
        sum(net_amount)   filter (where is_revenue)                                   as revenue_all_time
    from {{ ref('fct_order_items') }}
    group by vendor_id
)
select
    v.vendor_id,
    v.vendor_name,
    v.city,
    v.rating,
    v.is_autopilot,
    coalesce(s.revenue_30d, 0)        as revenue_30d,
    coalesce(s.margin_30d, 0)         as margin_30d,
    coalesce(s.units_30d, 0)          as units_30d,
    coalesce(sh.shipments_30d, 0)     as shipments_30d,
    coalesce(sh.delivered_30d, 0)     as delivered_30d,
    sh.cancel_rate,
    sh.return_rate,
    sh.avg_accept_minutes,
    sh.avg_prep_minutes,
    sh.avg_delivery_minutes,
    coalesce(s.revenue_all_time, 0)   as revenue_all_time,
    coalesce(sh.shipments_all_time, 0) as shipments_all_time,
    rank() over (order by coalesce(s.revenue_30d, 0) desc) as revenue_rank
from {{ ref('dim_vendor') }} v
left join ships sh using (vendor_id)
left join sales s using (vendor_id)
