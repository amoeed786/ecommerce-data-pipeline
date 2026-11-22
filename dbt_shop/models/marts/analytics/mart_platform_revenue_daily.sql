-- One row per day: what the marketplace earned. Commission is recognised when a parcel is delivered
-- (and given back when it's returned), so this is by delivery date, not order date.
with ledger as (
    select cast(created_at as date) as date_day,
           sum(gross_sales)       as delivered_sales,
           sum(platform_revenue)  as commission,
           count(distinct vendor_id) filter (where entry_type = 'sale') as selling_vendors
    from {{ ref('fct_ledger') }}
    group by 1
),
refunds as (
    select cast(updated_at as date) as date_day, sum(refunded_amount) as refunds
    from {{ source('silver', 'payments') }}
    where payment_status = 'refunded'
    group by 1
)
select
    d.date_day,
    coalesce(l.delivered_sales, 0)                                  as delivered_sales,
    coalesce(l.commission, 0)                                       as commission,
    round(l.commission / nullif(l.delivered_sales, 0), 4)           as take_rate,
    coalesce(l.selling_vendors, 0)                                  as selling_vendors,
    coalesce(r.refunds, 0)                                          as refunds,
    sum(coalesce(l.commission, 0)) over (order by d.date_day rows between 29 preceding and current row) as commission_30d
from {{ ref('dim_date') }} d
left join ledger l using (date_day)
left join refunds r using (date_day)
where d.date_day <= current_date
order by d.date_day
