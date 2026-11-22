-- A returned order must leave its sellers with nothing: sale + commission + both reversals sum to zero.
select l.order_id, sum(l.seller_amount) as seller_net, sum(l.platform_revenue) as platform_net
from {{ ref('fct_ledger') }} l
join {{ ref('fct_orders') }} o using (order_id)
where o.status = 'returned'
group by l.order_id
having abs(sum(l.seller_amount)) > 0.01 or abs(sum(l.platform_revenue)) > 0.01
