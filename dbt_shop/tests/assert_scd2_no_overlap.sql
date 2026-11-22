-- SCD2 windows of the same key must touch but never overlap or leave gaps.
with p as (
    select product_id as id, valid_to, lead(valid_from) over (partition by product_id order by valid_from) as next_from
    from {{ ref('dim_product') }}
),
c as (
    select customer_id as id, valid_to, lead(valid_from) over (partition by customer_id order by valid_from) as next_from
    from {{ ref('dim_customer') }}
)
select 'product' as dim, * from p where next_from is not null and next_from <> valid_to
union all
select 'customer', * from c where next_from is not null and next_from <> valid_to
