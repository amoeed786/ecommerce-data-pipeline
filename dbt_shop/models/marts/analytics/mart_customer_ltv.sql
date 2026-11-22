-- Customer lifetime value.
--   historical_ltv          what the customer has spent so far
--   predicted_12m_revenue   simple projection: average order value x orders-per-year pace
--                           (pace is measured since the first order, at least 30 days, capped at 52/yr)
with orders as (
    select
        customer_id,
        min(order_ts)                   as first_order_at,
        max(order_ts)                   as last_order_at,
        count(*)                        as orders,
        sum(net_revenue)                as revenue,
        sum(gross_margin)               as gross_margin
    from {{ ref('fct_orders') }}
    where is_revenue
    group by customer_id
)
select
    o.customer_id,
    c.full_name,
    c.country,
    c.signup_date,
    o.first_order_at,
    o.last_order_at,
    o.orders,
    o.revenue                                                         as historical_ltv,
    o.gross_margin                                                    as historical_margin,
    round(o.revenue / o.orders, 2)                                    as avg_order_value,
    date_diff('day', o.first_order_at, current_timestamp)             as customer_age_days,
    least(o.orders * 365.0 / greatest(date_diff('day', o.first_order_at, current_timestamp), 30), 52)
                                                                      as orders_per_year,
    round(o.revenue / o.orders
          * least(o.orders * 365.0 / greatest(date_diff('day', o.first_order_at, current_timestamp), 30), 52), 2)
                                                                      as predicted_12m_revenue,
    ntile(10) over (order by o.revenue desc)                          as value_decile   -- 1 = top 10%
from orders o
join {{ ref('dim_customer') }} c on c.customer_id = o.customer_id and c.is_current
