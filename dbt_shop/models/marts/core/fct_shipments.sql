-- Grain: one parcel (shipment). The fulfilment fact: how long each step took, who did it, what it carried.
-- Stage durations are in minutes; null when the step hasn't happened (yet).
with lines as (
    select shipment_id, count(*) as line_count, sum(quantity) as units, sum(line_total) as parcel_value
    from {{ ref('stg_order_items') }}
    group by shipment_id
)
select
    s.shipment_id,
    s.order_id,
    s.vendor_id,
    s.rider_id,
    o.customer_id,
    cast(strftime(cast(s.created_at as date), '%Y%m%d') as integer)   as created_date_key,
    s.status,
    s.status = 'delivered'                                             as is_delivered,
    s.status = 'cancelled'                                             as is_cancelled,
    s.status = 'returned'                                              as is_returned,
    coalesce(l.line_count, 0)                                          as line_count,
    coalesce(l.units, 0)                                               as units,
    coalesce(l.parcel_value, 0)                                        as parcel_value,
    s.delivery_fee,
    s.created_at, s.confirmed_at, s.accepted_at, s.ready_at, s.assigned_at, s.picked_up_at, s.delivered_at,
    date_diff('second', s.created_at,   s.confirmed_at) / 60.0         as minutes_to_confirm,     -- waiting for admin / auto-approve
    date_diff('second', s.confirmed_at, s.accepted_at)  / 60.0         as minutes_to_accept,      -- store reacts
    date_diff('second', s.accepted_at,  s.ready_at)     / 60.0         as minutes_to_pack,        -- store packs
    date_diff('second', s.ready_at,     s.assigned_at)  / 60.0         as minutes_waiting_rider,  -- dispatch
    date_diff('second', s.assigned_at,  s.picked_up_at) / 60.0         as minutes_to_pickup,      -- rider reaches store
    date_diff('second', s.picked_up_at, s.delivered_at) / 60.0         as minutes_ride,           -- rider to customer
    date_diff('second', s.confirmed_at, s.delivered_at) / 60.0         as minutes_confirm_to_door
from {{ ref('stg_shipments') }} s
join {{ ref('stg_orders') }} o using (order_id)
left join lines l using (shipment_id)
