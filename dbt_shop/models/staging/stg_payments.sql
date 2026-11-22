select
    payment_id,
    order_id,
    payment_method,
    amount,
    payment_status,
    paid_at,
    updated_at
from {{ source('silver', 'payments') }}
