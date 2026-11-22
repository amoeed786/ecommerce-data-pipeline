-- Grain: one row per order. Order header + rolled-up lines + payment.
with lines as (
    select
        order_id,
        count(*)            as line_count,
        sum(quantity)       as units,
        sum(gross_amount)   as gross_amount,
        sum(discount_amount) as discount_amount,
        sum(net_amount)     as lines_net_amount,
        sum(cost_amount)    as cost_amount,
        sum(gross_margin)   as gross_margin
    from {{ ref('fct_order_items') }}
    group by order_id
)
select
    o.order_id,
    cast(strftime(o.order_date, '%Y%m%d') as integer)  as order_date_key,
    o.order_ts,
    c.customer_sk,
    o.customer_id,
    o.status,
    o.is_revenue,
    o.shipping_city,
    o.shipping_country,
    coalesce(l.line_count, 0)                          as line_count,
    coalesce(l.units, 0)                               as units,
    l.gross_amount,
    l.discount_amount,
    o.total_amount,
    case when o.is_revenue then o.total_amount else 0 end as net_revenue,
    l.cost_amount,
    case when o.is_revenue then l.gross_margin else 0 end as gross_margin,
    p.payment_method,
    p.payment_status,
    p.paid_at,
    date_diff('minute', o.order_ts, p.paid_at)          as minutes_to_pay
from {{ ref('stg_orders') }} o
left join lines l using (order_id)
left join {{ ref('stg_payments') }} p using (order_id)
left join {{ ref('dim_customer') }} c
       on c.customer_id = o.customer_id and o.order_ts >= c.valid_from and o.order_ts < c.valid_to
