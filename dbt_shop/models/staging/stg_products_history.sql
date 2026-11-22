-- One row per captured version of a product (price changes show up here).
select
    product_id,
    category_id,
    product_name,
    sku,
    unit_price,
    unit_cost,
    is_active,
    vendor_id,
    created_at,
    updated_at
from {{ source('silver', 'products_history') }}
