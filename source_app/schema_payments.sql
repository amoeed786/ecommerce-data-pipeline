-- ShopDB v3: online payments + seller commission, ledger and payouts. Additive, safe to re-run.
--
-- Money flow:
--   customer pays (gateway, or cash to the rider) -> the platform holds it
--   parcel delivered -> seller ledger: +sale, -commission  (status 'pending' during the return window)
--   return window over -> entries become 'available'
--   admin runs payouts -> available entries are grouped into a payout -> marked 'paid' when the transfer is done
--   returned order -> reversal entries (-sale, +commission) cancel the seller's earnings for it

-- Payments: which gateway handled it and the gateway's reference, so refunds and callbacks can find it.
IF COL_LENGTH('dbo.payments', 'provider') IS NULL
    ALTER TABLE dbo.payments ADD provider VARCHAR(20) NULL;          -- sandbox | jazzcash | easypaisa | cod | simulator
IF COL_LENGTH('dbo.payments', 'provider_ref') IS NULL
    ALTER TABLE dbo.payments ADD provider_ref VARCHAR(100) NULL;     -- gateway session / transaction id
IF COL_LENGTH('dbo.payments', 'refunded_amount') IS NULL
    ALTER TABLE dbo.payments ADD refunded_amount DECIMAL(12,2) NOT NULL CONSTRAINT DF_payments_refunded DEFAULT 0;
IF COL_LENGTH('dbo.payments', 'failure_reason') IS NULL
    ALTER TABLE dbo.payments ADD failure_reason NVARCHAR(200) NULL;
GO

-- Every gateway callback, stored before it's processed. event_id is unique, so a gateway retrying the same
-- notification can never mark an order paid (or refunded) twice.
IF OBJECT_ID(N'dbo.payment_events', N'U') IS NULL
CREATE TABLE dbo.payment_events (
    event_id      VARCHAR(100)  PRIMARY KEY,
    provider      VARCHAR(20)   NOT NULL,
    event_type    VARCHAR(40)   NOT NULL,          -- payment.succeeded | payment.failed | refund.succeeded
    provider_ref  VARCHAR(100)  NULL,
    order_id      INT           NULL,
    amount        DECIMAL(12,2) NULL,
    received_at   DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
    processed_at  DATETIME2(0)  NULL,
    result        NVARCHAR(200) NULL,
    updated_at    DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME()
);
GO

-- Commission rates. Most specific wins: vendor > category > default. Rates are fractions (0.10 = 10%).
IF OBJECT_ID(N'dbo.commission_rules', N'U') IS NULL
CREATE TABLE dbo.commission_rules (
    rule_id      INT IDENTITY(1,1) PRIMARY KEY,
    scope        VARCHAR(10)   NOT NULL CHECK (scope IN ('default','category','vendor')),
    category_id  INT           NULL REFERENCES dbo.categories(category_id),
    vendor_id    INT           NULL REFERENCES dbo.vendors(vendor_id),
    rate         DECIMAL(5,4)  NOT NULL CHECK (rate >= 0 AND rate <= 0.5),
    created_at   DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
    updated_at   DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
    CONSTRAINT CK_commission_scope CHECK (
        (scope = 'default'  AND category_id IS NULL AND vendor_id IS NULL) OR
        (scope = 'category' AND category_id IS NOT NULL AND vendor_id IS NULL) OR
        (scope = 'vendor'   AND vendor_id IS NOT NULL AND category_id IS NULL))
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UQ_commission_rules_target')
    CREATE UNIQUE INDEX UQ_commission_rules_target ON dbo.commission_rules(scope, category_id, vendor_id);
GO

IF OBJECT_ID(N'dbo.payouts', N'U') IS NULL
CREATE TABLE dbo.payouts (
    payout_id    INT IDENTITY(1,1) PRIMARY KEY,
    vendor_id    INT           NOT NULL REFERENCES dbo.vendors(vendor_id),
    amount       DECIMAL(12,2) NOT NULL CHECK (amount > 0),
    entry_count  INT           NOT NULL,
    status       VARCHAR(10)   NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled','paid','failed')),
    reference    NVARCHAR(100) NULL,                -- bank transfer reference, entered when paid
    created_at   DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
    paid_at      DATETIME2(0)  NULL,
    updated_at   DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME()
);
GO

-- Seller ledger. Amounts are signed from the seller's point of view: + money the platform owes the seller,
-- - money taken back (commission, refunds). A seller's balance is simply SUM(amount).
IF OBJECT_ID(N'dbo.ledger_entries', N'U') IS NULL
CREATE TABLE dbo.ledger_entries (
    entry_id        INT IDENTITY(1,1) PRIMARY KEY,
    vendor_id       INT           NOT NULL REFERENCES dbo.vendors(vendor_id),
    order_id        INT           NOT NULL REFERENCES dbo.orders(order_id),
    shipment_id     INT           NULL REFERENCES dbo.shipments(shipment_id),
    order_item_id   INT           NULL REFERENCES dbo.order_items(order_item_id),
    entry_type      VARCHAR(20)   NOT NULL CHECK (entry_type IN ('sale','commission','sale_reversal','commission_reversal','adjustment')),
    amount          DECIMAL(12,2) NOT NULL,
    commission_rate DECIMAL(5,4)  NULL,             -- the rate in force at the sale - later rate changes never rewrite history
    status          VARCHAR(10)   NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','available','paid')),
    available_at    DATETIME2(0)  NOT NULL,         -- end of the return window
    payout_id       INT           NULL REFERENCES dbo.payouts(payout_id),
    created_at      DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
    updated_at      DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME()
);
GO
-- One sale and one commission entry per order line at most: replaying a delivery can't double-pay.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UQ_ledger_line_type')
    CREATE UNIQUE INDEX UQ_ledger_line_type ON dbo.ledger_entries(order_item_id, entry_type) WHERE order_item_id IS NOT NULL;
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_ledger_vendor_status')
    CREATE INDEX IX_ledger_vendor_status ON dbo.ledger_entries(vendor_id, status) INCLUDE (amount, available_at);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_ledger_updated_at')
    CREATE INDEX IX_ledger_updated_at ON dbo.ledger_entries(updated_at);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_payouts_updated_at')
    CREATE INDEX IX_payouts_updated_at ON dbo.payouts(updated_at);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_commission_rules_updated_at')
    CREATE INDEX IX_commission_rules_updated_at ON dbo.commission_rules(updated_at);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_payments_provider_ref')
    CREATE INDEX IX_payments_provider_ref ON dbo.payments(provider_ref);
GO
