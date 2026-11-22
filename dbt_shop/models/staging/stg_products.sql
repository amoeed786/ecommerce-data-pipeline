select
    product_id,
    category_id,
    product_name,
    sku,
    unit_price,
    unit_cost,
    is_active,
    vendor_id,
    list_price,
    rating,
    review_count,
    created_at,
    updated_at
from {{ source('silver', 'products') }}
