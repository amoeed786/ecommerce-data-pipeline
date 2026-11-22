-- SCD2 customer dimension: a new row every time the customer moved (city/country) or changed name/email.
{{ scd2(ref('stg_customers_history'), 'customer_id',
        ['first_name', 'last_name', 'email', 'city', 'country']) }}
