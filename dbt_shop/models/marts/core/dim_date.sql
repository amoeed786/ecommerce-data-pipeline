-- Calendar dimension: one row per day from the first order to 90 days past today.
with bounds as (
    select min(order_date) as lo, greatest(max(order_date), current_date) + 90 as hi
    from {{ ref('stg_orders') }}
),
days as (
    select cast(d as date) as date_day
    from bounds, range(bounds.lo, bounds.hi, interval 1 day) as t(d)
)
select
    cast(strftime(date_day, '%Y%m%d') as integer) as date_key,
    date_day,
    year(date_day)                               as year,
    quarter(date_day)                            as quarter,
    month(date_day)                              as month,
    monthname(date_day)                          as month_name,
    strftime(date_day, '%Y-%m')                  as year_month,
    weekofyear(date_day)                         as iso_week,
    day(date_day)                                as day_of_month,
    isodow(date_day)                             as day_of_week,    -- 1 = Monday
    dayname(date_day)                            as day_name,
    isodow(date_day) in (6, 7)                   as is_weekend,
    date_day = current_date                      as is_today
from days
