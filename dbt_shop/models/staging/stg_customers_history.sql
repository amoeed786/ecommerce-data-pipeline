-- One row per captured version of a customer (a new row each time city/country/name/email changed
-- AND an extract ran in between).
select
    customer_id,
    first_name,
    last_name,
    first_name || ' ' || last_name as full_name,
    email,
    city,
    country,
    signup_date,
    created_at,
    updated_at
from {{ source('silver', 'customers_history') }}
