-- One row per product (current version): sales, margin, returns, rank and current stock.
with sales as (
    select
        product_id,
        sum(quantity)      filter (where is_revenue)        as units_sold,
        sum(net_amount)    filter (where is_revenue)        as revenue,
        sum(gross_margin)  filter (where is_revenue)        as gross_margin,
        count(distinct order_id)                            as orders,
        count(distinct order_id) filter (where status = 'returned') as returned_orders,
        max(order_ts)                                       as last_sold_at
    from {{ ref('fct_order_items') }}
    group by product_id
),
reviews as (
    select
        product_id,
        count(*)                                            as written_reviews,
        round(avg(rating), 2)                               as avg_review_rating,
        round(avg(case when rating <= 2 then 1.0 else 0 end), 4) as low_rating_share
    from {{ ref('stg_reviews') }}
    group by product_id
)
select
    p.product_id,
    p.product_name,
    p.sku,
    p.category_name,
    p.unit_price                                        as current_price,
    p.version                                           as price_versions,
    coalesce(s.units_sold, 0)                           as units_sold,
    coalesce(s.revenue, 0)                              as revenue,
    coalesce(s.gross_margin, 0)                         as gross_margin,
    round(s.gross_margin / nullif(s.revenue, 0), 4)     as margin_pct,
    coalesce(s.orders, 0)                               as orders,
    round(s.returned_orders / nullif(s.orders, 0), 4)   as return_rate,
    s.last_sold_at,
    i.quantity_on_hand                                  as stock,
    coalesce(r.written_reviews, 0)                      as written_reviews,
    r.avg_review_rating,
    r.low_rating_share,
    rank() over (order by coalesce(s.revenue, 0) desc)  as revenue_rank,
    rank() over (partition by p.category_name order by coalesce(s.revenue, 0) desc) as category_rank
from {{ ref('dim_product') }} p
left join sales s using (product_id)
left join {{ ref('stg_inventory') }} i using (product_id)
left join reviews r using (product_id)
where p.is_current
