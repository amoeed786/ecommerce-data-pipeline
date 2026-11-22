select
    entry_id,
    vendor_id,
    order_id,
    shipment_id,
    order_item_id,
    entry_type,
    amount,
    commission_rate,
    status,
    available_at,
    payout_id,
    created_at
from {{ source('silver', 'ledger_entries') }}
