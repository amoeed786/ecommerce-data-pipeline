select
    shipment_id,
    order_id,
    vendor_id,
    rider_id,
    status,
    delivery_fee,
    created_at,
    confirmed_at,
    accepted_at,
    ready_at,
    assigned_at,
    picked_up_at,
    delivered_at,
    cancelled_at,
    updated_at
from {{ source('silver', 'shipments') }}
