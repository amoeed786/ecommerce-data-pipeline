select
    rider_id,
    full_name,
    city,
    vehicle,
    status,
    is_bot as is_autopilot,
    created_at,
    updated_at
from {{ source('silver', 'riders') }}
