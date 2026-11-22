select
    payout_id,
    vendor_id,
    amount,
    entry_count,
    status,
    created_at,
    paid_at
from {{ source('silver', 'payouts') }}
