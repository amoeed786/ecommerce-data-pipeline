-- Revenue and margin per category per month, with month-over-month growth.
with monthly as (
    select
        p.category_name,
        d.year_month,
        sum(f.net_amount)   filter (where f.is_revenue) as revenue,
        sum(f.gross_margin) filter (where f.is_revenue) as gross_margin,
        sum(f.quantity)     filter (where f.is_revenue) as units,
        count(distinct f.order_id)                      as orders
    from {{ ref('fct_order_items') }} f
    join {{ ref('dim_product') }} p using (product_sk)
    join {{ ref('dim_date') }} d on d.date_key = f.order_date_key
    group by all
)
select
    *,
    lag(revenue) over (partition by category_name order by year_month) as prev_month_revenue,
    round(revenue / nullif(lag(revenue) over (partition by category_name order by year_month), 0) - 1, 4)
                                                                      as mom_growth,
    round(revenue / sum(revenue) over (partition by year_month), 4)   as share_of_month
from monthly
order by year_month, revenue desc
