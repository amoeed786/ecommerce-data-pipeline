-- Vendor dimension (type 1 - current values). One row per store.
select
    v.vendor_id,
    v.vendor_name,
    v.tagline,
    v.city,
    v.country,
    v.rating,
    v.is_active,
    v.is_autopilot,
    v.created_at,
    (select count(*) from {{ ref('stg_users') }} u where u.vendor_id = v.vendor_id) > 0 as has_login
from {{ ref('stg_vendors') }} v
