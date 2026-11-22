-- Every customer and product must have exactly one current version. Returns offending keys.
select 'customer' as dim, customer_id as id, count(*) filter (where is_current) as current_versions
from {{ ref('dim_customer') }} group by customer_id
having count(*) filter (where is_current) <> 1
union all
select 'product', product_id, count(*) filter (where is_current)
from {{ ref('dim_product') }} group by product_id
having count(*) filter (where is_current) <> 1
