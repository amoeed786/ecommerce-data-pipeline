select
    product_id,
    quantity_on_hand,
    updated_at
from {{ source('silver', 'inventory') }}
