-- Monthly cohorts: customers grouped by the month of their first order. For each later month,
-- how many of them ordered again? Read a row as "of the N customers who started in <cohort>,
-- X% bought in month <months_since_first>".
with activity as (
    select distinct customer_id, date_trunc('month', order_ts) as active_month
    from {{ ref('fct_orders') }}
    where is_revenue
),
cohorts as (
    select customer_id, min(active_month) as cohort_month
    from activity
    group by customer_id
),
sizes as (
    select cohort_month, count(*) as cohort_size from cohorts group by cohort_month
)
select
    strftime(c.cohort_month, '%Y-%m')                               as cohort,
    date_diff('month', c.cohort_month, a.active_month)              as months_since_first,
    s.cohort_size,
    count(distinct a.customer_id)                                   as active_customers,
    round(count(distinct a.customer_id) / s.cohort_size, 4)         as retention_pct
from activity a
join cohorts c using (customer_id)
join sizes s using (cohort_month)
group by c.cohort_month, a.active_month, s.cohort_size
order by cohort, months_since_first
