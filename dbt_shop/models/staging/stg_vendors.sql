select
    vendor_id,
    vendor_name,
    tagline,
    city,
    country,
    rating,
    is_active,
    is_bot as is_autopilot,
    prep_seconds,
    created_at,
    updated_at
from {{ source('silver', 'vendors') }}
