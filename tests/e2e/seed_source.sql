-- Source database for the end-to-end suite: every common type, identity, composite keys,
-- computed/rowversion columns, FKs, checks, filtered index, views/procs/functions/triggers,
-- sequences, alias types and ~1.1M rows so chunking and parallelism are exercised.
IF DB_ID('e2e_src') IS NOT NULL BEGIN ALTER DATABASE e2e_src SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE e2e_src; END
GO
CREATE DATABASE e2e_src;
GO
USE e2e_src;
GO
CREATE SCHEMA sales;
GO
CREATE TYPE dbo.Phone FROM varchar(20) NULL;
GO
CREATE SEQUENCE dbo.InvoiceNo AS bigint START WITH 1000 INCREMENT BY 1;
GO
CREATE TABLE sales.Customer (
    CustomerId   int IDENTITY(100, 7) NOT NULL CONSTRAINT PK_Customer PRIMARY KEY CLUSTERED,
    Code         varchar(12) COLLATE Latin1_General_CS_AS NOT NULL CONSTRAINT UQ_Customer_Code UNIQUE,
    Name         nvarchar(200) NOT NULL,
    Phone        dbo.Phone,
    Notes        nvarchar(max) NULL,
    IsActive     bit NOT NULL CONSTRAINT DF_Customer_IsActive DEFAULT (1),
    CreatedAt    datetime2(7) NOT NULL CONSTRAINT DF_Customer_CreatedAt DEFAULT (SYSUTCDATETIME()),
    LegacyStamp  datetime NULL,
    Rv           rowversion,
    CONSTRAINT CK_Customer_Name CHECK (LEN(Name) > 0)
);
CREATE TABLE sales.[Order] (
    OrderId      bigint IDENTITY(1, 1) NOT NULL CONSTRAINT PK_Order PRIMARY KEY CLUSTERED,
    CustomerId   int NOT NULL CONSTRAINT FK_Order_Customer REFERENCES sales.Customer(CustomerId),
    OrderGuid    uniqueidentifier NOT NULL CONSTRAINT DF_Order_Guid DEFAULT NEWID(),
    OrderDate    date NOT NULL,
    OrderTime    time(7) NULL,
    PlacedAt     datetimeoffset(7) NOT NULL,
    Amount       decimal(19, 4) NOT NULL,
    Tax          money NULL,
    Discount     smallmoney NULL,
    Rate         float NULL,
    Ratio        real NULL,
    Status       tinyint NOT NULL,
    Priority     smallint NULL,
    Total        AS (Amount + ISNULL(Tax, 0)) PERSISTED,
    Payload      varbinary(max) NULL,
    Hash         binary(16) NULL,
    Tag          char(3) NULL,
    Meta         xml NULL
);
CREATE INDEX IX_Order_Customer ON sales.[Order](CustomerId, OrderDate DESC) INCLUDE (Amount);
CREATE INDEX IX_Order_Open ON sales.[Order](Status) WHERE Status < 3;
CREATE TABLE sales.OrderLine (
    OrderId   bigint NOT NULL,
    [LineNo]    int NOT NULL,
    Sku       nvarchar(40) NOT NULL,
    Qty       int NOT NULL,
    Price     numeric(12, 2) NOT NULL,
    CONSTRAINT PK_OrderLine PRIMARY KEY (OrderId, [LineNo]),
    CONSTRAINT FK_OrderLine_Order FOREIGN KEY (OrderId) REFERENCES sales.[Order](OrderId) ON DELETE CASCADE
);
CREATE TABLE dbo.Place (Id int NOT NULL PRIMARY KEY, Loc geography NULL, Node hierarchyid NULL);
CREATE TABLE dbo.EventLog (At datetime2(3) NOT NULL, Source sysname NOT NULL, Message nvarchar(4000) NULL); -- heap, no key
CREATE TABLE dbo.Empty (Id int NOT NULL PRIMARY KEY, V varchar(10) NULL);
GO
-- data ---------------------------------------------------------------------------------------
SET NOCOUNT ON;
WITH n AS (SELECT TOP (20000) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS i
           FROM sys.all_objects a CROSS JOIN sys.all_objects b)
INSERT sales.Customer (Code, Name, Phone, Notes, IsActive, CreatedAt, LegacyStamp)
SELECT CONCAT(CASE WHEN i % 2 = 0 THEN 'c' ELSE 'C' END, RIGHT('0000000' + CAST(i AS varchar(8)), 8)),
       CONCAT(N'Kunde ', i, N' – Ünïcødé 漢字 😀'),
       CASE WHEN i % 5 = 0 THEN NULL ELSE CONCAT('+1-555-', i) END,
       CASE WHEN i % 50 = 0 THEN REPLICATE(CONVERT(nvarchar(max), N'long note '), 2000) END,
       i % 2,
       DATEADD(NANOSECOND, (i % 9999999) * 100, DATEADD(SECOND, i, CAST('2023-01-01' AS datetime2(7)))),
       DATEADD(MILLISECOND, i * 3, CAST('2020-01-01' AS datetime))
FROM n;
GO
DELETE FROM sales.Customer WHERE CustomerId % 13 = 0;    -- identity gaps must survive
GO
WITH n AS (SELECT TOP (600000) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS i
           FROM sys.all_objects a CROSS JOIN sys.all_objects b CROSS JOIN sys.all_objects c),
     cu AS (SELECT CustomerId, ROW_NUMBER() OVER (ORDER BY CustomerId) - 1 AS rn, COUNT(*) OVER () AS cnt
            FROM sales.Customer)
INSERT sales.[Order] (CustomerId, OrderDate, OrderTime, PlacedAt, Amount, Tax, Discount, Rate, Ratio, Status,
                      Priority, Payload, Hash, Tag, Meta)
SELECT cu.CustomerId,
       DATEADD(DAY, i % 3650, '2015-01-01'),
       CASE WHEN i % 7 = 0 THEN NULL ELSE DATEADD(NANOSECOND, (i % 86399) * 100, CAST('12:34:56.1234567' AS time(7))) END,
       TODATETIMEOFFSET(DATEADD(NANOSECOND, i * 100, CAST('2024-03-10 01:59:59.9999999' AS datetime2(7))), (i % 27 - 13) * 60),
       CAST(i AS decimal(19, 4)) / 7,
       CASE WHEN i % 3 = 0 THEN NULL ELSE CAST(i % 1000 AS money) / 3 END,
       CAST(i % 100 AS smallmoney) / 7,
       CASE WHEN i % 11 = 0 THEN NULL ELSE SQRT(i) * PI() END,
       CAST(i AS real) / 3,
       i % 5,
       CASE WHEN i % 4 = 0 THEN NULL ELSE i % 30000 END,
       CASE WHEN i % 1000 = 0 THEN CAST(REPLICATE(CAST(0xDEADBEEF AS varbinary(max)), 5000) AS varbinary(max))
            WHEN i % 10 = 0 THEN NULL ELSE CAST(i AS varbinary(8)) END,
       HASHBYTES('MD5', CAST(i AS varchar(12))),
       CASE WHEN i % 9 = 0 THEN NULL ELSE 'AB' END,
       CASE WHEN i % 500 = 0 THEN CAST(CONCAT('<order id="', i, '"><note>x &amp; y</note></order>') AS xml) END
FROM n JOIN cu ON cu.rn = n.i % cu.cnt;
GO
DELETE FROM sales.[Order] WHERE OrderId BETWEEN 1000 AND 1999;    -- gap
GO
INSERT sales.OrderLine (OrderId, [LineNo], Sku, Qty, Price)
SELECT o.OrderId, l.n, CONCAT(N'SKU-', (o.OrderId * l.n) % 9973), l.n, CAST((o.OrderId % 500) AS numeric(12,2)) / 3
FROM sales.[Order] o CROSS JOIN (VALUES (1), (2), (3)) l(n)
WHERE o.OrderId % 4 <> 0 AND o.OrderId <= 300000;
GO
INSERT dbo.Place (Id, Loc, Node)
SELECT i, geography::Point(47.6 + i / 1000.0, -122.3, 4326), hierarchyid::Parse(CONCAT('/', i % 10 + 1, '/', i, '/'))
FROM (SELECT TOP (2000) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS i FROM sys.all_objects) x;
INSERT dbo.Place (Id) VALUES (999999);
GO
INSERT dbo.EventLog (At, Source, Message)
SELECT DATEADD(MILLISECOND, i, '2024-01-01'), N'svc', CASE WHEN i % 3 = 0 THEN NULL ELSE CONCAT(N'event ', i) END
FROM (SELECT TOP (50000) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS i
      FROM sys.all_objects a CROSS JOIN sys.all_objects b) x;
INSERT dbo.EventLog (At, Source, Message) SELECT TOP (10) At, Source, Message FROM dbo.EventLog; -- duplicate rows
GO
SELECT NEXT VALUE FOR dbo.InvoiceNo; SELECT NEXT VALUE FOR dbo.InvoiceNo;
GO
CREATE VIEW sales.vOrderTotals AS
    SELECT c.Code, COUNT_BIG(*) AS Orders, SUM(o.Amount) AS Amount
    FROM sales.[Order] o JOIN sales.Customer c ON c.CustomerId = o.CustomerId GROUP BY c.Code;
GO
CREATE VIEW sales.vTopCustomers AS SELECT TOP (10) Code, Amount FROM sales.vOrderTotals ORDER BY Amount DESC;
GO
CREATE FUNCTION dbo.fnNet (@amount decimal(19,4), @tax money) RETURNS decimal(19,4)
AS BEGIN RETURN @amount - ISNULL(@tax, 0); END;
GO
CREATE PROCEDURE sales.usp_CustomerOrders @CustomerId int AS
    SELECT OrderId, dbo.fnNet(Amount, Tax) AS Net FROM sales.[Order] WHERE CustomerId = @CustomerId;
GO
CREATE TRIGGER sales.trg_Order_Audit ON sales.[Order] AFTER UPDATE AS SET NOCOUNT ON;
GO
DISABLE TRIGGER sales.trg_Order_Audit ON sales.[Order];
GO
CREATE TRIGGER sales.trg_Customer_Touch ON sales.Customer AFTER INSERT AS SET NOCOUNT ON;
GO
CREATE SYNONYM dbo.Orders FOR sales.[Order];
GO
-- 0.3.0: storage options, statistics, extended properties, security, database settings ------
CREATE INDEX IX_Order_Date ON sales.[Order](OrderDate)
    WITH (FILLFACTOR = 80, PAD_INDEX = ON, DATA_COMPRESSION = PAGE, ALLOW_PAGE_LOCKS = OFF);
CREATE UNIQUE INDEX IX_OrderLine_Sku ON sales.OrderLine(OrderId, [LineNo], Sku) WITH (IGNORE_DUP_KEY = ON);
ALTER INDEX PK_Customer ON sales.Customer REBUILD WITH (DATA_COMPRESSION = ROW, FILLFACTOR = 90);
ALTER TABLE dbo.EventLog REBUILD WITH (DATA_COMPRESSION = ROW);            -- compressed heap
CREATE STATISTICS st_Order_Rate ON sales.[Order](Rate, Ratio) WHERE Rate IS NOT NULL WITH NORECOMPUTE;
GO
EXEC sys.sp_addextendedproperty N'Owner', N'Sales data team';
EXEC sys.sp_addextendedproperty N'MS_Description', N'Shop sales', N'SCHEMA', N'sales';
EXEC sys.sp_addextendedproperty N'MS_Description', N'Customers of the shop', N'SCHEMA', N'sales', N'TABLE', N'Customer';
EXEC sys.sp_addextendedproperty N'MS_Description', N'Net amount, 4 decimals', N'SCHEMA', N'sales', N'TABLE', N'Order', N'COLUMN', N'Amount';
EXEC sys.sp_addextendedproperty N'MS_Description', N'Open orders only', N'SCHEMA', N'sales', N'TABLE', N'Order', N'INDEX', N'IX_Order_Open';
EXEC sys.sp_addextendedproperty N'MS_Description', N'Order key', N'SCHEMA', N'sales', N'TABLE', N'Order', N'CONSTRAINT', N'PK_Order';
EXEC sys.sp_addextendedproperty N'MS_Description', N'Totals per customer', N'SCHEMA', N'sales', N'VIEW', N'vOrderTotals';
GO
CREATE ROLE reporting;
CREATE USER etl_svc WITHOUT LOGIN WITH DEFAULT_SCHEMA = sales;
ALTER ROLE reporting ADD MEMBER etl_svc;
ALTER ROLE db_datareader ADD MEMBER etl_svc;
GRANT SELECT ON sales.vOrderTotals TO reporting;
GRANT EXECUTE ON sales.usp_CustomerOrders TO reporting;
DENY SELECT ON sales.Customer (Notes) TO reporting;
GRANT SELECT ON SCHEMA::sales TO etl_svc WITH GRANT OPTION;
GRANT VIEW DEFINITION TO reporting;
GO
CREATE SCHEMA audit AUTHORIZATION etl_svc;      -- empty schema owned by a user
GO
ALTER DATABASE e2e_src SET COMPATIBILITY_LEVEL = 150;
ALTER DATABASE e2e_src SET ALLOW_SNAPSHOT_ISOLATION ON;
ALTER DATABASE e2e_src SET READ_COMMITTED_SNAPSHOT ON WITH ROLLBACK IMMEDIATE;
GO
-- 0.3.0 (continued): everything else inside the database ------------------------------------
ALTER DATABASE e2e_src ADD FILEGROUP e2e_mod CONTAINS MEMORY_OPTIMIZED_DATA;
GO
ALTER DATABASE e2e_src ADD FILE (NAME = N'e2e_mod', FILENAME = N'/var/opt/mssql/data/e2e_src_mod') TO FILEGROUP e2e_mod;
GO
ALTER DATABASE e2e_src SET CHANGE_TRACKING = ON (CHANGE_RETENTION = 3 DAYS, AUTO_CLEANUP = ON);
ALTER DATABASE SCOPED CONFIGURATION SET MAXDOP = 2;
GO
CREATE XML SCHEMA COLLECTION dbo.NoteSchema AS N'<xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <xsd:element name="note" type="xsd:string"/></xsd:schema>';
GO
CREATE TYPE dbo.IdList AS TABLE (Id int NOT NULL PRIMARY KEY, Note nvarchar(50) NULL DEFAULT (N'-'));
GO
CREATE PROCEDURE dbo.usp_Count @ids dbo.IdList READONLY AS SELECT COUNT(*) FROM @ids;
GO
CREATE TABLE dbo.Contact (
    Id     int NOT NULL CONSTRAINT PK_Contact PRIMARY KEY,
    Email  nvarchar(200) MASKED WITH (FUNCTION = 'email()') NULL,
    Phone  varchar(20) MASKED WITH (FUNCTION = 'partial(2, "XXXX", 2)') NULL,
    Fax    varchar(20) SPARSE NULL,
    Pager  varchar(20) SPARSE NULL,
    Extra  xml COLUMN_SET FOR ALL_SPARSE_COLUMNS,
    Note   xml(DOCUMENT dbo.NoteSchema) NULL,
    Rep    sysname NOT NULL CONSTRAINT DF_Contact_Rep DEFAULT (N'alice')
);
INSERT dbo.Contact (Id, Email, Phone, Fax, Note, Rep)
SELECT i, CONCAT(N'user', i, N'@example.com'), CONCAT('555-01', i % 100), CASE WHEN i % 3 = 0 THEN 'F1' END,
       CASE WHEN i % 2 = 0 THEN N'<note>hello</note>' END, CASE WHEN i % 2 = 0 THEN N'alice' ELSE N'bob' END
FROM (SELECT TOP (3000) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS i FROM sys.all_objects a CROSS JOIN sys.all_objects b) x;
ALTER TABLE dbo.Contact ENABLE CHANGE_TRACKING WITH (TRACK_COLUMNS_UPDATED = ON);
GO
CREATE SCHEMA rls;
GO
CREATE FUNCTION rls.fn_rep (@Rep sysname) RETURNS TABLE WITH SCHEMABINDING
AS RETURN SELECT 1 AS ok WHERE @Rep = USER_NAME() OR USER_NAME() = N'dbo';
GO
CREATE SECURITY POLICY rls.ContactByRep ADD FILTER PREDICATE rls.fn_rep(Rep) ON dbo.Contact,
    ADD BLOCK PREDICATE rls.fn_rep(Rep) ON dbo.Contact AFTER INSERT WITH (STATE = ON);
GO
CREATE PARTITION FUNCTION pf_year (date) AS RANGE RIGHT FOR VALUES ('2016-01-01', '2020-01-01', '2024-01-01');
CREATE PARTITION SCHEME ps_year AS PARTITION pf_year ALL TO ([PRIMARY]);
GO
CREATE TABLE dbo.Ledger (
    Id     bigint NOT NULL,
    Booked date NOT NULL,
    Amount decimal(19, 4) NOT NULL,
    CONSTRAINT PK_Ledger PRIMARY KEY CLUSTERED (Id, Booked) ON ps_year(Booked)
);
INSERT dbo.Ledger (Id, Booked, Amount)
SELECT i, DATEADD(DAY, i % 4000, '2014-01-01'), i / 3.0
FROM (SELECT TOP (60000) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS i FROM sys.all_objects a CROSS JOIN sys.all_objects b) x;
ALTER INDEX PK_Ledger ON dbo.Ledger REBUILD PARTITION = 1 WITH (DATA_COMPRESSION = PAGE);
ALTER INDEX PK_Ledger ON dbo.Ledger REBUILD PARTITION = 2 WITH (DATA_COMPRESSION = ROW);
CREATE INDEX IX_Ledger_Amount ON dbo.Ledger (Amount) ON ps_year(Booked);
GO
CREATE PRIMARY XML INDEX PX_Order_Meta ON sales.[Order] (Meta);
GO
CREATE XML INDEX SX_Order_Meta_Path ON sales.[Order] (Meta) USING XML INDEX PX_Order_Meta FOR PATH;
CREATE SPATIAL INDEX SI_Place_Loc ON dbo.Place (Loc) USING GEOGRAPHY_AUTO_GRID WITH (CELLS_PER_OBJECT = 12);
GO
CREATE TABLE dbo.Price (
    Sku       nvarchar(40) NOT NULL CONSTRAINT PK_Price PRIMARY KEY,
    Amount    decimal(12, 2) NOT NULL,
    ValidFrom datetime2(7) GENERATED ALWAYS AS ROW START HIDDEN NOT NULL,
    ValidTo   datetime2(7) GENERATED ALWAYS AS ROW END HIDDEN NOT NULL,
    PERIOD FOR SYSTEM_TIME (ValidFrom, ValidTo)
) WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = dbo.PriceHistory, HISTORY_RETENTION_PERIOD = 6 MONTHS));
GO
INSERT dbo.Price (Sku, Amount) SELECT CONCAT(N'SKU-', i), i FROM
    (SELECT TOP (500) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS i FROM sys.all_objects) x;
GO
UPDATE dbo.Price SET Amount = Amount + 1 WHERE Amount < 200;
GO
UPDATE dbo.Price SET Amount = Amount * 2 WHERE Amount < 50;
GO
CREATE TABLE dbo.Session (
    Id      bigint NOT NULL CONSTRAINT PK_Session PRIMARY KEY NONCLUSTERED HASH WITH (BUCKET_COUNT = 16384),
    UserId  int NOT NULL,
    Started datetime2(3) NOT NULL,
    INDEX IX_Session_User NONCLUSTERED (UserId)
) WITH (MEMORY_OPTIMIZED = ON, DURABILITY = SCHEMA_AND_DATA);
GO
INSERT dbo.Session (Id, UserId, Started) SELECT i, i % 97, DATEADD(SECOND, i, '2024-01-01') FROM
    (SELECT TOP (5000) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS i FROM sys.all_objects a CROSS JOIN sys.all_objects b) x;
GO
CREATE RULE dbo.rPositive AS @v > 0;
GO
EXEC sys.sp_bindrule N'dbo.rPositive', N'sales.OrderLine.Qty';
GO
CREATE MESSAGE TYPE [//e2e/Order] VALIDATION = WELL_FORMED_XML;
CREATE CONTRACT [//e2e/OrderContract] ([//e2e/Order] SENT BY INITIATOR);
CREATE QUEUE dbo.OrderQueue WITH STATUS = ON, RETENTION = OFF;
CREATE SERVICE [//e2e/OrderService] ON QUEUE dbo.OrderQueue ([//e2e/OrderContract]);
GRANT SEND ON SERVICE::[//e2e/OrderService] TO reporting;
GO
CREATE MASTER KEY ENCRYPTION BY PASSWORD = 'E2e!MasterKey1';
CREATE CERTIFICATE SignCert WITH SUBJECT = 'e2e module signing';
CREATE USER signer FROM CERTIFICATE SignCert;
GO
CREATE PROCEDURE dbo.usp_Signed AS SELECT COUNT(*) FROM sales.Customer;
GO
ADD SIGNATURE TO dbo.usp_Signed BY CERTIFICATE SignCert;
GRANT SELECT ON sales.Customer TO signer;
GO
CREATE TRIGGER ddl_noop ON DATABASE FOR CREATE_PROCEDURE AS SET NOCOUNT ON;
GO
EXEC sys.sp_create_plan_guide @name = N'pg_marker', @stmt = N'SELECT 1 AS planguide_marker', @type = N'SQL',
     @module_or_batch = NULL, @params = NULL, @hints = N'OPTION (MAXDOP 1)';
GO
IF SUSER_ID('e2e_app') IS NULL CREATE LOGIN e2e_app WITH PASSWORD = 'E2e!AppLogin1', CHECK_POLICY = OFF;
GO
CREATE USER e2e_app FOR LOGIN e2e_app;
ALTER ROLE reporting ADD MEMBER e2e_app;
GO
EXEC sys.sp_cdc_enable_db;
GO
EXEC sys.sp_cdc_enable_table @source_schema = N'sales', @source_name = N'Customer', @role_name = NULL,
     @supports_net_changes = 1, @captured_column_list = N'CustomerId, Code, Name';
GO
IF EXISTS (SELECT 1 FROM msdb.dbo.sysjobs WHERE name = N'e2e nightly') EXEC msdb.dbo.sp_delete_job @job_name = N'e2e nightly';
EXEC msdb.dbo.sp_add_job @job_name = N'e2e nightly', @enabled = 1;
EXEC msdb.dbo.sp_add_jobstep @job_name = N'e2e nightly', @step_name = N'count', @subsystem = N'TSQL',
     @database_name = N'e2e_src', @command = N'SELECT COUNT(*) FROM sales.Customer';
EXEC msdb.dbo.sp_add_jobschedule @job_name = N'e2e nightly', @name = N'daily', @freq_type = 4, @freq_interval = 1,
     @active_start_time = 20000;
EXEC msdb.dbo.sp_add_jobserver @job_name = N'e2e nightly';
GO
IF NOT EXISTS (SELECT 1 FROM sys.servers WHERE name = N'E2E_LOOP')
    EXEC master.dbo.sp_addlinkedserver @server = N'E2E_LOOP', @srvproduct = N'SQL Server';
EXEC master.dbo.sp_serveroption N'E2E_LOOP', N'rpc out', N'true';
GO
