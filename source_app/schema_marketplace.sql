-- ShopDB v2: marketplace. Vendors sell products, riders deliver, users log in.
-- Additive and safe to re-run: only creates what is missing. Run by setup_db.py after schema.sql.
--
-- Order flow (one SHIPMENT per vendor in an order):
--   shipment: pending -> confirmed (admin "start") -> packing (vendor accepted) -> ready (packed)
--             -> assigned (rider) -> picked_up -> delivered          (or cancelled / returned)
--   order:    pending -> processing -> shipped (all picked up) -> delivered (all delivered)

IF OBJECT_ID(N'dbo.vendors', N'U') IS NULL
CREATE TABLE dbo.vendors (
    vendor_id     INT IDENTITY(1,1) PRIMARY KEY,
    vendor_name   NVARCHAR(100) NOT NULL UNIQUE,
    tagline       NVARCHAR(200) NULL,
    city          NVARCHAR(100) NULL,
    country       NVARCHAR(100) NULL,
    rating        DECIMAL(2,1)  NOT NULL DEFAULT 4.5,
    is_active     BIT           NOT NULL DEFAULT 1,
    is_bot        BIT           NOT NULL DEFAULT 1,   -- autopilot: accepts and packs orders by itself
    prep_seconds  INT           NOT NULL DEFAULT 30,  -- simulated packing time for the autopilot
    created_at    DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
    updated_at    DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME()
);
GO

IF OBJECT_ID(N'dbo.riders', N'U') IS NULL
CREATE TABLE dbo.riders (
    rider_id    INT IDENTITY(1,1) PRIMARY KEY,
    full_name   NVARCHAR(100) NOT NULL,
    phone       NVARCHAR(30)  NULL,
    city        NVARCHAR(100) NULL,
    vehicle     VARCHAR(10)   NOT NULL DEFAULT 'bike' CHECK (vehicle IN ('bike','car','van')),
    status      VARCHAR(10)   NOT NULL DEFAULT 'available' CHECK (status IN ('available','busy','offline')),
    is_bot      BIT           NOT NULL DEFAULT 1,     -- autopilot: picks up and delivers by itself
    created_at  DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
    updated_at  DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME()
);
GO

IF OBJECT_ID(N'dbo.shipments', N'U') IS NULL
CREATE TABLE dbo.shipments (
    shipment_id   INT IDENTITY(1,1) PRIMARY KEY,
    order_id      INT           NOT NULL REFERENCES dbo.orders(order_id),
    vendor_id     INT           NOT NULL REFERENCES dbo.vendors(vendor_id),
    rider_id      INT           NULL REFERENCES dbo.riders(rider_id),
    status        VARCHAR(12)   NOT NULL DEFAULT 'pending' CHECK (status IN
                  ('pending','confirmed','packing','ready','assigned','picked_up','delivered','cancelled','returned')),
    delivery_fee  DECIMAL(8,2)  NOT NULL DEFAULT 0,   -- paid by the platform to the rider
    confirmed_at  DATETIME2(0)  NULL,
    accepted_at   DATETIME2(0)  NULL,
    ready_at      DATETIME2(0)  NULL,
    assigned_at   DATETIME2(0)  NULL,
    picked_up_at  DATETIME2(0)  NULL,
    delivered_at  DATETIME2(0)  NULL,
    cancelled_at  DATETIME2(0)  NULL,
    created_at    DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
    updated_at    DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
    CONSTRAINT UQ_shipments_order_vendor UNIQUE (order_id, vendor_id)
);
GO

IF OBJECT_ID(N'dbo.users', N'U') IS NULL
CREATE TABLE dbo.users (
    user_id        INT IDENTITY(1,1) PRIMARY KEY,
    email          NVARCHAR(255) NOT NULL UNIQUE,
    password_hash  VARCHAR(255)  NOT NULL,           -- scrypt; never extracted by the pipeline
    role           VARCHAR(10)   NOT NULL CHECK (role IN ('customer','vendor','rider','admin')),
    display_name   NVARCHAR(100) NOT NULL,
    customer_id    INT NULL REFERENCES dbo.customers(customer_id),
    vendor_id      INT NULL REFERENCES dbo.vendors(vendor_id),
    rider_id       INT NULL REFERENCES dbo.riders(rider_id),
    is_active      BIT           NOT NULL DEFAULT 1,
    last_login_at  DATETIME2(0)  NULL,
    created_at     DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
    updated_at     DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME()
);
GO

IF OBJECT_ID(N'dbo.user_sessions', N'U') IS NULL
CREATE TABLE dbo.user_sessions (
    token_hash  CHAR(64)     PRIMARY KEY,             -- sha256 of the cookie token; the raw token is never stored
    user_id     INT          NOT NULL REFERENCES dbo.users(user_id),
    created_at  DATETIME2(0) NOT NULL DEFAULT SYSDATETIME(),
    expires_at  DATETIME2(0) NOT NULL
);
GO

IF OBJECT_ID(N'dbo.app_settings', N'U') IS NULL
CREATE TABLE dbo.app_settings (
    setting_key    VARCHAR(50)   PRIMARY KEY,
    setting_value  NVARCHAR(200) NOT NULL,
    updated_at     DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME()
);
GO

-- New product columns: who sells it, and what a storefront shows.
IF COL_LENGTH('dbo.products', 'vendor_id') IS NULL
    ALTER TABLE dbo.products ADD vendor_id INT NULL CONSTRAINT FK_products_vendor REFERENCES dbo.vendors(vendor_id);
IF COL_LENGTH('dbo.products', 'list_price') IS NULL
    ALTER TABLE dbo.products ADD list_price DECIMAL(10,2) NULL;       -- "was" price shown struck through
IF COL_LENGTH('dbo.products', 'rating') IS NULL
    ALTER TABLE dbo.products ADD rating DECIMAL(2,1) NULL;
IF COL_LENGTH('dbo.products', 'review_count') IS NULL
    ALTER TABLE dbo.products ADD review_count INT NOT NULL CONSTRAINT DF_products_review_count DEFAULT 0;
IF COL_LENGTH('dbo.products', 'description') IS NULL
    ALTER TABLE dbo.products ADD description NVARCHAR(400) NULL;
IF COL_LENGTH('dbo.products', 'image_url') IS NULL
    ALTER TABLE dbo.products ADD image_url NVARCHAR(300) NULL;       -- product photo (seller upload or stock photo)
IF COL_LENGTH('dbo.order_items', 'shipment_id') IS NULL
    ALTER TABLE dbo.order_items ADD shipment_id INT NULL CONSTRAINT FK_order_items_shipment REFERENCES dbo.shipments(shipment_id);
GO

-- Orders get one new status: 'processing' (admin started it, vendors are preparing).
IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_orders_status_v2')
BEGIN
    DECLARE @old SYSNAME = (SELECT TOP 1 name FROM sys.check_constraints
                            WHERE parent_object_id = OBJECT_ID('dbo.orders') AND definition LIKE '%pending%');
    IF @old IS NOT NULL EXEC('ALTER TABLE dbo.orders DROP CONSTRAINT [' + @old + ']');
    ALTER TABLE dbo.orders ADD CONSTRAINT CK_orders_status_v2
        CHECK (status IN ('pending','processing','paid','shipped','delivered','cancelled','returned'));
END
GO

-- Payments get 'pending': cash on delivery is only collected when the rider delivers.
IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_payments_status_v2')
BEGIN
    DECLARE @oldp SYSNAME = (SELECT TOP 1 name FROM sys.check_constraints
                             WHERE parent_object_id = OBJECT_ID('dbo.payments') AND definition LIKE '%refunded%');
    IF @oldp IS NOT NULL EXEC('ALTER TABLE dbo.payments DROP CONSTRAINT [' + @oldp + ']');
    ALTER TABLE dbo.payments ADD CONSTRAINT CK_payments_status_v2
        CHECK (payment_status IN ('pending','completed','failed','refunded'));
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_shipments_updated_at')
    CREATE INDEX IX_shipments_updated_at ON dbo.shipments(updated_at);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_shipments_status')
    CREATE INDEX IX_shipments_status ON dbo.shipments(status) INCLUDE (vendor_id, rider_id, order_id);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_shipments_order_id')
    CREATE INDEX IX_shipments_order_id ON dbo.shipments(order_id);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_shipments_vendor_id')
    CREATE INDEX IX_shipments_vendor_id ON dbo.shipments(vendor_id);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_shipments_rider_id')
    CREATE INDEX IX_shipments_rider_id ON dbo.shipments(rider_id);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_products_vendor_id')
    CREATE INDEX IX_products_vendor_id ON dbo.products(vendor_id);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_users_updated_at')
    CREATE INDEX IX_users_updated_at ON dbo.users(updated_at);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_vendors_updated_at')
    CREATE INDEX IX_vendors_updated_at ON dbo.vendors(updated_at);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_riders_updated_at')
    CREATE INDEX IX_riders_updated_at ON dbo.riders(updated_at);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_order_items_shipment_id')
    CREATE INDEX IX_order_items_shipment_id ON dbo.order_items(shipment_id);
GO
