-- Every order line must ship in a parcel from the store that sells the product.
select f.order_item_id, f.vendor_id as shipment_vendor, p.vendor_id as product_vendor
from {{ ref('fct_order_items') }} f
join {{ ref('stg_products') }} p using (product_id)
where f.vendor_id <> p.vendor_id
