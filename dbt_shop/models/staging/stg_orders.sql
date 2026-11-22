select
    order_id,
    customer_id,
    order_date                       as order_ts,
    cast(order_date as date)         as order_date,
    status,
    status not in (
        {%- for s in var('non_revenue_statuses') %}'{{ s }}'{{ "," if not loop.last }}{% endfor -%}
    )                                as is_revenue,
    shipping_city,
    shipping_country,
    total_amount,
    created_at,
    updated_at
from {{ source('silver', 'orders') }}
