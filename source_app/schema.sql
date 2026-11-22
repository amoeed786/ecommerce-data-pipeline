-- ShopDB: OLTP schema for the fake ecommerce application.
-- Every table carries updated_at so the ingestion layer (Phase 2) can load incrementally.
-- Batches are separated by GO; setup_db.py runs them in order. Safe to re-run.

IF OBJECT_ID(N'dbo.categories', N'U') IS NULL
CREATE TABLE dbo.categories (
    category_id   INT IDENTITY(1,1) PRIMARY KEY,
    category_name NVARCHAR(100) NOT NULL UNIQUE,
    created_at    DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
    updated_at    DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME()
);
GO

IF OBJECT_ID(N'dbo.customers', N'U') IS NULL
CREATE TABLE dbo.customers (
    customer_id INT IDENTITY(1,1) PRIMARY KEY,
    first_name  NVARCHAR(50)  NOT NULL,
    last_name   NVARCHAR(50)  NOT NULL,
    email       NVARCHAR(255) NOT NULL UNIQUE,
    phone       NVARCHAR(30)  NULL,
    city        NVARCHAR(100) NULL,
    country     NVARCHAR(100) NULL,
    signup_date DATE          NOT NULL,
    created_at  DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
    updated_at  DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME()
);
GO

IF OBJECT_ID(N'dbo.products', N'U') IS NULL
CREATE TABLE dbo.products (
    product_id   INT IDENTITY(1,1) PRIMARY KEY,
    category_id  INT           NOT NULL REFERENCES dbo.categories(category_id),
    product_name NVARCHAR(200) NOT NULL,
    sku          NVARCHAR(20)  NOT NULL UNIQUE,
    unit_price   DECIMAL(10,2) NOT NULL CHECK (unit_price >= 0),
    unit_cost    DECIMAL(10,2) NOT NULL CHECK (unit_cost >= 0),
    is_active    BIT           NOT NULL DEFAULT 1,
    created_at   DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
    updated_at   DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME()
);
GO

IF OBJECT_ID(N'dbo.inventory', N'U') IS NULL
CREATE TABLE dbo.inventory (
    product_id       INT          PRIMARY KEY REFERENCES dbo.products(product_id),
    quantity_on_hand INT          NOT NULL,
    updated_at       DATETIME2(0) NOT NULL DEFAULT SYSDATETIME()
);
GO

IF OBJECT_ID(N'dbo.orders', N'U') IS NULL
CREATE TABLE dbo.orders (
    order_id         INT IDENTITY(1,1) PRIMARY KEY,
    customer_id      INT           NOT NULL REFERENCES dbo.customers(customer_id),
    order_date       DATETIME2(0)  NOT NULL,
    status           VARCHAR(20)   NOT NULL
        CHECK (status IN ('pending','paid','shipped','delivered','cancelled','returned')),
    shipping_city    NVARCHAR(100) NULL,
    shipping_country NVARCHAR(100) NULL,
    total_amount     DECIMAL(12,2) NOT NULL,
    created_at       DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
    updated_at       DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME()
);
GO

IF OBJECT_ID(N'dbo.order_items', N'U') IS NULL
CREATE TABLE dbo.order_items (
    order_item_id INT IDENTITY(1,1) PRIMARY KEY,
    order_id      INT           NOT NULL REFERENCES dbo.orders(order_id),
    product_id    INT           NOT NULL REFERENCES dbo.products(product_id),
    quantity      INT           NOT NULL CHECK (quantity > 0),
    unit_price    DECIMAL(10,2) NOT NULL,
    discount      DECIMAL(4,2)  NOT NULL DEFAULT 0 CHECK (discount BETWEEN 0 AND 0.9),
    line_total    DECIMAL(12,2) NOT NULL,
    created_at    DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
    updated_at    DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME()
);
GO

IF OBJECT_ID(N'dbo.payments', N'U') IS NULL
CREATE TABLE dbo.payments (
    payment_id     INT IDENTITY(1,1) PRIMARY KEY,
    order_id       INT           NOT NULL REFERENCES dbo.orders(order_id),
    payment_method VARCHAR(30)   NOT NULL,
    amount         DECIMAL(12,2) NOT NULL,
    payment_status VARCHAR(20)   NOT NULL CHECK (payment_status IN ('completed','failed','refunded')),
    paid_at        DATETIME2(0)  NOT NULL,
    created_at     DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
    updated_at     DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME()
);
GO

-- Indexes: updated_at for incremental extraction, plus common join keys.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_customers_updated_at')
    CREATE INDEX IX_customers_updated_at ON dbo.customers(updated_at);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_products_updated_at')
    CREATE INDEX IX_products_updated_at ON dbo.products(updated_at);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_inventory_updated_at')
    CREATE INDEX IX_inventory_updated_at ON dbo.inventory(updated_at);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_orders_updated_at')
    CREATE INDEX IX_orders_updated_at ON dbo.orders(updated_at);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_orders_customer_id')
    CREATE INDEX IX_orders_customer_id ON dbo.orders(customer_id);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_orders_status')
    CREATE INDEX IX_orders_status ON dbo.orders(status);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_order_items_updated_at')
    CREATE INDEX IX_order_items_updated_at ON dbo.order_items(updated_at);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_order_items_order_id')
    CREATE INDEX IX_order_items_order_id ON dbo.order_items(order_id);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_payments_updated_at')
    CREATE INDEX IX_payments_updated_at ON dbo.payments(updated_at);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_payments_order_id')
    CREATE INDEX IX_payments_order_id ON dbo.payments(order_id);
GO
