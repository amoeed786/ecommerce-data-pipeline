-- Product history joined to its category, ready for SCD2.
select
    h.product_id,
    h.product_name,
    h.sku,
    h.category_id,
    c.category_name,
    h.unit_price,
    h.unit_cost,
    h.is_active,
    h.vendor_id,          -- carried along, not SCD2-tracked: the sale's vendor comes from its shipment
    h.created_at,
    h.updated_at
from {{ ref('stg_products_history') }} h
left join {{ ref('stg_categories') }} c using (category_id)
