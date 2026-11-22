select
    review_id,
    product_id,
    customer_id,
    cast(rating as integer) as rating,
    title,
    body,
    cast(verified as boolean) as is_verified,
    helpful,
    created_at
from {{ source('silver', 'reviews') }}
