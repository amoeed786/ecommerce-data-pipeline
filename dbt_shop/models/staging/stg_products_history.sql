-- One row per captured version of a product (price changes show up here).
-- Prices are restated into the current reporting currency: versions captured before a currency switch
-- (seed currency_restatements, e.g. USD -> PKR at 280) are multiplied by that switch's rate, so a
-- point-in-time join never subtracts a USD cost from PKR revenue.
with fx as (
    select cast(switched_at as timestamp) as switched_at, cast(rate as decimal(12, 4)) as rate
    from {{ ref('currency_restatements') }}
),
h as (
    select
        h.*,
        coalesce((select exp(sum(ln(fx.rate))) from fx where h.updated_at < fx.switched_at), 1) as fx_rate
    from {{ source('silver', 'products_history') }} h
)
select
    product_id,
    category_id,
    product_name,
    sku,
    round(unit_price * fx_rate, 2) as unit_price,
    round(unit_cost * fx_rate, 2)  as unit_cost,
    is_active,
    vendor_id,
    created_at,
    updated_at
from h
