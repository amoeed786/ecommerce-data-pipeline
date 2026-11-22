-- One row per seller: what they've sold, what the platform kept, and where their money is right now.
select
    v.vendor_id,
    v.vendor_name,
    v.is_autopilot,
    coalesce(sum(l.gross_sales), 0)                                                     as gross_sales,
    coalesce(sum(l.platform_revenue), 0)                                                as commission_paid,
    round(sum(l.platform_revenue) / nullif(sum(l.gross_sales), 0), 4)                   as effective_rate,
    coalesce(sum(l.seller_amount) filter (where l.status = 'pending'), 0)               as held_in_return_window,
    coalesce(sum(l.seller_amount) filter (where l.status = 'available'), 0)             as available,
    coalesce(sum(l.seller_amount) filter (where l.status = 'paid'), 0)                  as paid_out,
    (select count(*) from {{ ref('stg_payouts') }} p where p.vendor_id = v.vendor_id and p.status = 'paid') as payouts_paid,
    (select max(paid_at) from {{ ref('stg_payouts') }} p where p.vendor_id = v.vendor_id)                  as last_payout_at
from {{ ref('dim_vendor') }} v
left join {{ ref('fct_ledger') }} l using (vendor_id)
group by v.vendor_id, v.vendor_name, v.is_autopilot
