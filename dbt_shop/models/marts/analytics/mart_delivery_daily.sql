-- One row per day (by delivery date): how many parcels were delivered and where the time went.
select
    cast(delivered_at as date)                  as date_day,
    count(*)                                    as deliveries,
    count(distinct rider_id)                    as riders_active,
    count(distinct vendor_id)                   as vendors_active,
    sum(delivery_fee)                           as rider_payouts,
    round(avg(minutes_to_accept), 1)            as avg_accept_minutes,
    round(avg(minutes_to_pack), 1)              as avg_pack_minutes,
    round(avg(minutes_waiting_rider), 1)        as avg_wait_rider_minutes,
    round(avg(minutes_to_pickup), 1)            as avg_pickup_minutes,
    round(avg(minutes_ride), 1)                 as avg_ride_minutes,
    round(avg(minutes_confirm_to_door), 1)      as avg_total_minutes,
    round(quantile_cont(minutes_confirm_to_door, 0.9), 1) as p90_total_minutes
from {{ ref('fct_shipments') }}
where delivered_at is not null
group by cast(delivered_at as date)
order by date_day
