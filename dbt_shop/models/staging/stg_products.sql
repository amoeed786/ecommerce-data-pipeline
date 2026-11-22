select
    product_id,
    category_id,
    product_name,
    sku,
    unit_price,
    unit_cost,
    is_active,
    created_at,
    updated_at
from {{ source('silver', 'products') }}
