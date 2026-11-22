-- Grain: one ledger entry. Signed from the seller's side: + owed to the seller, - taken back.
-- platform_revenue flips the commission rows so the platform's take reads as a positive number.
select
    l.entry_id,
    l.vendor_id,
    l.order_id,
    l.shipment_id,
    l.order_item_id,
    cast(strftime(cast(l.created_at as date), '%Y%m%d') as integer)        as entry_date_key,
    l.entry_type,
    l.amount                                                               as seller_amount,
    case when l.entry_type in ('commission', 'commission_reversal') then -l.amount else 0 end as platform_revenue,
    case when l.entry_type in ('sale', 'sale_reversal') then l.amount else 0 end              as gross_sales,
    l.commission_rate,
    l.status,
    l.available_at,
    l.payout_id,
    l.created_at
from {{ ref('stg_ledger_entries') }} l
