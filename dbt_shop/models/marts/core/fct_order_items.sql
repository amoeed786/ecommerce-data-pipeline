-- Grain: one row per order line. The main fact table.
-- Customer and product are joined AS THEY WERE when the order was placed (SCD2 point-in-time join),
-- so cost and margin use the product's cost on that day, and geography uses where the customer lived.
select
    i.order_item_id,
    i.order_id,
    cast(strftime(o.order_date, '%Y%m%d') as integer)   as order_date_key,
    o.order_ts,
    c.customer_sk,
    p.product_sk,
    o.customer_id,
    i.product_id,
    o.status,
    o.is_revenue,
    i.quantity,
    i.unit_price,
    i.discount,
    round(i.quantity * i.unit_price, 2)                 as gross_amount,
    round(i.quantity * i.unit_price, 2) - i.line_total  as discount_amount,
    i.line_total                                        as net_amount,
    round(i.quantity * p.unit_cost, 2)                  as cost_amount,
    i.line_total - round(i.quantity * p.unit_cost, 2)   as gross_margin
from {{ ref('stg_order_items') }} i
join {{ ref('stg_orders') }} o using (order_id)
left join {{ ref('dim_product') }} p
       on p.product_id = i.product_id and o.order_ts >= p.valid_from and o.order_ts < p.valid_to
left join {{ ref('dim_customer') }} c
       on c.customer_id = o.customer_id and o.order_ts >= c.valid_from and o.order_ts < c.valid_to
