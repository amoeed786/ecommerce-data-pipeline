-- RFM segmentation: Recency (days since last order), Frequency (orders), Monetary (revenue).
-- Each is scored 1-5 by quintile (5 = best), then combined into a named segment.
with base as (
    select
        customer_id,
        max(order_date_key)                       as last_order_key,
        count(*)                                  as frequency,
        sum(net_revenue)                          as monetary
    from {{ ref('fct_orders') }}
    where is_revenue
    group by customer_id
),
measured as (
    select
        b.*,
        date_diff('day', d.date_day, current_date) as recency_days
    from base b
    join {{ ref('dim_date') }} d on d.date_key = b.last_order_key
),
scored as (
    select
        *,
        ntile(5) over (order by recency_days desc) as r_score,   -- recent buyers get 5
        ntile(5) over (order by frequency)         as f_score,
        ntile(5) over (order by monetary)          as m_score
    from measured
)
select
    s.customer_id,
    c.full_name,
    c.email,
    c.city,
    c.country,
    s.recency_days,
    s.frequency,
    s.monetary,
    s.r_score, s.f_score, s.m_score,
    cast(s.r_score as varchar) || s.f_score || s.m_score as rfm_code,
    case
        when s.r_score >= 4 and s.f_score >= 4 then 'Champions'
        when s.r_score >= 3 and s.f_score >= 4 then 'Loyal'
        when s.r_score >= 4 and s.f_score between 2 and 3 then 'Potential loyalist'
        when s.r_score >= 4 and s.f_score = 1 then 'New customer'
        when s.r_score <= 2 and s.f_score >= 4 then 'Cannot lose them'
        when s.r_score <= 2 and s.f_score = 3 then 'At risk'
        when s.r_score <= 2 and s.f_score <= 2 then 'Hibernating'
        else 'Needs attention'
    end as segment
from scored s
join {{ ref('dim_customer') }} c on c.customer_id = s.customer_id and c.is_current
