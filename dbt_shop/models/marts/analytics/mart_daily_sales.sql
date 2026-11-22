-- One row per calendar day: the shop's daily scoreboard (days without orders show zeros).
with daily as (
    select
        order_date_key,
        count(*)                                        as orders,
        count(*) filter (where is_revenue)              as revenue_orders,
        count(*) filter (where status = 'cancelled')    as cancelled_orders,
        count(*) filter (where status = 'returned')     as returned_orders,
        count(distinct customer_id)                     as customers,
        sum(units) filter (where is_revenue)            as units,
        sum(net_revenue)                                as revenue,
        sum(gross_margin)                               as gross_margin,
        sum(discount_amount) filter (where is_revenue)  as discounts
    from {{ ref('fct_orders') }}
    group by order_date_key
)
select
    d.date_day,
    d.year_month,
    d.day_name,
    d.is_weekend,
    coalesce(s.orders, 0)            as orders,
    coalesce(s.revenue_orders, 0)    as revenue_orders,
    coalesce(s.cancelled_orders, 0)  as cancelled_orders,
    coalesce(s.returned_orders, 0)   as returned_orders,
    coalesce(s.customers, 0)         as customers,
    coalesce(s.units, 0)             as units,
    coalesce(s.revenue, 0)           as revenue,
    coalesce(s.gross_margin, 0)      as gross_margin,
    coalesce(s.discounts, 0)         as discounts,
    round(s.revenue / nullif(s.revenue_orders, 0), 2)  as avg_order_value,
    round(s.gross_margin / nullif(s.revenue, 0), 4)    as margin_pct,
    sum(coalesce(s.revenue, 0)) over (order by d.date_day rows between 6 preceding and current row) / 7
                                                       as revenue_7d_avg
from {{ ref('dim_date') }} d
left join daily s on s.order_date_key = d.date_key
where d.date_day <= current_date
order by d.date_day
