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
    LineNo    int NOT NULL,
    Sku       nvarchar(40) NOT NULL,
    Qty       int NOT NULL,
    Price     numeric(12, 2) NOT NULL,
    CONSTRAINT PK_OrderLine PRIMARY KEY (OrderId, LineNo),
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
INSERT sales.OrderLine (OrderId, LineNo, Sku, Qty, Price)
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
