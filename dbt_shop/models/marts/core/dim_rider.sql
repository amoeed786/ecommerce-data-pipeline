-- Rider dimension (type 1 - current values).
select
    rider_id,
    full_name,
    city,
    vehicle,
    is_autopilot,
    created_at
from {{ ref('stg_riders') }}
