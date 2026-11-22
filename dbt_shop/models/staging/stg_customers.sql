select
    customer_id,
    first_name,
    last_name,
    first_name || ' ' || last_name as full_name,
    email,
    phone,
    city,
    country,
    signup_date,
    created_at,
    updated_at
from {{ source('silver', 'customers') }}
