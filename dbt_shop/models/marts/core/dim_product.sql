-- SCD2 product dimension: a new row every time price, cost, name, category or active flag changed.
{{ scd2(ref('int_product_versions'), 'product_id',
        ['product_name', 'sku', 'category_id', 'unit_price', 'unit_cost', 'is_active']) }}
