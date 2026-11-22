-- Bonus: 30-day revenue forecast, in plain SQL.
-- Model = linear trend fitted on the last 90 complete days  x  day-of-week seasonality factor.
-- Simple and explainable; good enough to show the shape. Output has actuals AND forecast rows.
with history as (
    select date_day, revenue, isodow(date_day) as dow,
           date_diff('day', current_date - 90, date_day) as t
    from {{ ref('mart_daily_sales') }}
    where date_day >= current_date - 90 and date_day < current_date      -- today is incomplete
),
trend as (
    select regr_slope(revenue, t) as slope, regr_intercept(revenue, t) as intercept, avg(revenue) as mean_revenue
    from history
),
seasonality as (
    select dow, avg(revenue) / (select mean_revenue from trend) as factor
    from history group by dow
),
future as (
    select cast(d as date) as date_day
    from range(current_date, current_date + 30, interval 1 day) as r(d)
)
select date_day, 'actual' as kind, revenue, null::double as forecast
from {{ ref('mart_daily_sales') }}
where date_day >= current_date - 90 and date_day < current_date
union all
select
    f.date_day,
    'forecast',
    null,
    round(greatest((tr.intercept + tr.slope * date_diff('day', current_date - 90, f.date_day)) * coalesce(s.factor, 1), 0), 2)
from future f
cross join trend tr
left join seasonality s on s.dow = isodow(f.date_day)
order by date_day
