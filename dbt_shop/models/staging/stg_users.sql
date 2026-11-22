select
    user_id,
    role,
    customer_id,
    vendor_id,
    rider_id,
    is_active,
    last_login_at,
    created_at
from {{ source('silver', 'users') }}
