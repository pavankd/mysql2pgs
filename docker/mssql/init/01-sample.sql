-- Sample database for the Schema Dump page (run by the mssql-init service with sqlcmd -I).
-- Test fixture: the shop database is recreated on every run. (A RETURN guard would only
-- skip the current GO batch, not the rest of the script.)
IF DB_ID(N'shop') IS NOT NULL
BEGIN
  ALTER DATABASE shop SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
  DROP DATABASE shop;
END;
GO
CREATE DATABASE shop;
GO
USE shop;
GO
CREATE SCHEMA sales;
GO
CREATE SCHEMA hr;
GO
CREATE TYPE sales.email_t FROM nvarchar(191) NOT NULL;
GO
CREATE SEQUENCE sales.order_seq AS bigint START WITH 100 INCREMENT BY 1;
GO
CREATE TABLE sales.customers (
  id          int IDENTITY(1, 1) CONSTRAINT pk_customers PRIMARY KEY,
  email       sales.email_t CONSTRAINT uq_customers_email UNIQUE,
  full_name   nvarchar(120) NULL,
  bio         nvarchar(max) NULL,
  is_active   bit NOT NULL CONSTRAINT df_customers_active DEFAULT (1),
  balance     decimal(12, 2) NULL CONSTRAINT ck_customers_balance CHECK (balance >= 0),
  created_at  datetime2(3) NOT NULL CONSTRAINT df_customers_created DEFAULT SYSUTCDATETIME(),
  legacy_dt   datetime NULL,
  offset_ts   datetimeoffset(7) NULL,
  guid        uniqueidentifier NULL,
  avatar      varbinary(max) NULL,
  rv          rowversion,
  domain      AS (SUBSTRING(email, CHARINDEX(N'@', email) + 1, 191)) PERSISTED,
  home        geography NULL,
  node        hierarchyid NULL,
  profile     xml NULL,
  price       money NULL,
  ratio       float NULL,
  code        char(4) COLLATE Latin1_General_BIN2 NULL
);
CREATE TABLE sales.orders (
  id          bigint NOT NULL CONSTRAINT df_orders_id DEFAULT (NEXT VALUE FOR sales.order_seq) CONSTRAINT pk_orders PRIMARY KEY,
  customer_id int NOT NULL CONSTRAINT fk_orders_customer REFERENCES sales.customers (id) ON DELETE CASCADE,
  note        nvarchar(400) NULL,
  status      varchar(10) NOT NULL CONSTRAINT df_orders_status DEFAULT ('new')
);
CREATE NONCLUSTERED INDEX ix_orders_customer ON sales.orders (customer_id DESC) INCLUDE (status) WHERE status <> 'void';
CREATE TABLE hr.account_managers (
  name        nvarchar(50) NOT NULL,
  customer_id int NOT NULL CONSTRAINT fk_am_customer REFERENCES sales.customers (id)
);
CREATE TABLE dbo.settings ([key] nvarchar(50) NOT NULL PRIMARY KEY, [value] nvarchar(200) NULL);
GO
INSERT INTO sales.customers (email, full_name, bio, balance, legacy_dt, offset_ts, guid, avatar, home, node, profile, price, ratio, code)
VALUES (N'ada@example.com', N'Ada Lovelace 🚀', N'It''s' + CHAR(10) + N'GO' + CHAR(10) + N'done', 1234.50,
        '2024-01-02T03:04:05.123', '2024-01-02T03:04:05.1234567+02:00', 'B9E2F1A4-1C55-4C2B-9C3E-2B7A1D0E5F11',
        0x00FF10, geography::Point(51.5, -0.12, 4326), hierarchyid::Parse('/1/2/'), N'<p a="1">x</p>', 9.99, 0.1, 'AbC1'),
       (N'grace@example.com', N'Grace Hopper', NULL, 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
INSERT INTO sales.orders (customer_id, note) VALUES (1, NULL), (2, N'gift');
INSERT INTO hr.account_managers VALUES (N'Linus', 1);
INSERT INTO dbo.settings VALUES (N'theme', N'dark');
GO
CREATE VIEW sales.v_customer_orders AS
  SELECT c.email, COUNT(o.id) AS orders FROM sales.customers c LEFT JOIN sales.orders o ON o.customer_id = c.id GROUP BY c.email;
GO
CREATE FUNCTION sales.order_count (@customer int) RETURNS int AS
BEGIN
  RETURN (SELECT COUNT(*) FROM sales.orders WHERE customer_id = @customer);
END;
GO
CREATE PROCEDURE sales.top_customers AS
  SELECT TOP 10 email FROM sales.customers ORDER BY balance DESC;
GO
-- Fills a NULL note: if it fired during a restore, NULL notes would change.
CREATE TRIGGER sales.trg_orders_note ON sales.orders AFTER INSERT AS
  UPDATE o SET note = N'n/a' FROM sales.orders o JOIN inserted i ON i.id = o.id WHERE o.note IS NULL;
GO
-- Permission-check users
IF SUSER_ID(N'reporter') IS NULL CREATE LOGIN reporter WITH PASSWORD = 'Reporter_pass1', CHECK_POLICY = OFF;
CREATE USER reporter FOR LOGIN reporter;
GRANT SELECT ON sales.customers TO reporter;
IF SUSER_ID(N'auditor') IS NULL CREATE LOGIN auditor WITH PASSWORD = 'Auditor_pass1', CHECK_POLICY = OFF;
CREATE USER auditor FOR LOGIN auditor;
GRANT VIEW DEFINITION ON SCHEMA::sales TO auditor;
GO
