-- Sample schemas for the Schema Dump page (run by gvenzl/oracle-free as SYS on first start).
SET DEFINE OFF
WHENEVER SQLERROR EXIT FAILURE
ALTER SESSION SET CONTAINER = FREEPDB1;

ALTER USER shop QUOTA UNLIMITED ON users;
GRANT CREATE VIEW, CREATE MATERIALIZED VIEW, CREATE PROCEDURE, CREATE TRIGGER, CREATE SEQUENCE, CREATE TYPE, CREATE SYNONYM TO shop;

CREATE USER hr IDENTIFIED BY hr QUOTA UNLIMITED ON users;
GRANT CREATE SESSION, CREATE TABLE TO hr;

-- Can read one table of SHOP, nothing else: permission check must refuse SHOP.
CREATE USER reporter IDENTIFIED BY reporter;
GRANT CREATE SESSION TO reporter;

-- Catalog role (DDL of any schema) but no data access to other schemas.
CREATE USER auditor IDENTIFIED BY auditor;
GRANT CREATE SESSION, SELECT_CATALOG_ROLE TO auditor;

ALTER SESSION SET CURRENT_SCHEMA = shop;

CREATE TABLE customers (
  id          NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  email       VARCHAR2(191) NOT NULL CONSTRAINT uq_customers_email UNIQUE,
  full_name   NVARCHAR2(120),
  bio         CLOB,
  created_at  TIMESTAMP(6) DEFAULT SYSTIMESTAMP NOT NULL,
  birth_date  DATE,
  tz_ts       TIMESTAMP(6) WITH TIME ZONE,
  avatar      BLOB,
  ratio       BINARY_DOUBLE,
  balance     NUMBER(12,2) CONSTRAINT ck_balance CHECK (balance >= 0),
  wait_time   INTERVAL DAY(2) TO SECOND(6),
  token       RAW(16)
);
COMMENT ON TABLE customers IS 'Registered customers';
COMMENT ON COLUMN customers.email IS 'Login e-mail';

CREATE SEQUENCE order_seq START WITH 100;
CREATE TABLE orders (
  id          NUMBER DEFAULT order_seq.NEXTVAL PRIMARY KEY,
  customer_id NUMBER NOT NULL CONSTRAINT fk_orders_customer REFERENCES customers (id),
  note        VARCHAR2(4000),
  placed      DATE DEFAULT SYSDATE NOT NULL
);
CREATE INDEX ix_orders_customer ON orders (customer_id);

INSERT INTO customers (email, full_name, bio, birth_date, tz_ts, ratio, balance, wait_time, token)
VALUES ('ada@example.com', N'Ada Lovelace', 'Line one' || CHR(10) || '/' || CHR(10) || 'It''s & done', DATE '1815-12-10',
        TIMESTAMP '2024-01-02 03:04:05.123456 +02:00', 0.1, 1234.5, INTERVAL '1 02:03:04.5' DAY TO SECOND, HEXTORAW('00FF10'));
INSERT INTO customers (email, full_name, ratio, balance) VALUES ('grace@example.com', N'Grace Hopper 🚀', BINARY_DOUBLE_INFINITY, 0);
DECLARE
  big CLOB;
  img BLOB;
BEGIN
  DBMS_LOB.CREATETEMPORARY(big, TRUE);
  FOR i IN 1 .. 120 LOOP
    DBMS_LOB.APPEND(big, TO_CLOB(RPAD('chunk ' || i || ' ', 500, 'x')));
  END LOOP;
  DBMS_LOB.CREATETEMPORARY(img, TRUE);
  FOR i IN 1 .. 6 LOOP
    DBMS_LOB.APPEND(img, TO_BLOB(UTL_RAW.COPIES(HEXTORAW('DEADBEEF'), 250)));
  END LOOP;
  INSERT INTO customers (email, full_name, bio, avatar) VALUES ('alan@example.com', N'Alan Turing', big, img);
END;
/
INSERT INTO orders (customer_id, note) SELECT id, NULL FROM customers WHERE email = 'ada@example.com';
INSERT INTO orders (customer_id, note) SELECT id, 'gift' FROM customers WHERE email = 'alan@example.com';

CREATE VIEW v_customer_orders AS
  SELECT c.email, COUNT(o.id) AS orders FROM customers c LEFT JOIN orders o ON o.customer_id = c.id GROUP BY c.email;

CREATE OR REPLACE FUNCTION order_count(p_customer NUMBER) RETURN NUMBER IS
  n NUMBER;
BEGIN
  SELECT COUNT(*) INTO n FROM orders WHERE customer_id = p_customer;
  RETURN n;
END;
/
CREATE OR REPLACE PACKAGE greet AS
  FUNCTION hello RETURN VARCHAR2;
END greet;
/
CREATE OR REPLACE PACKAGE BODY greet AS
  FUNCTION hello RETURN VARCHAR2 IS BEGIN RETURN 'hello'; END;
END greet;
/
-- Fills a NULL note: if it fired during a restore, NULL notes would change.
CREATE OR REPLACE TRIGGER trg_orders_note BEFORE INSERT ON orders FOR EACH ROW
BEGIN
  IF :new.note IS NULL THEN :new.note := 'n/a'; END IF;
END;
/
GRANT SELECT ON customers TO reporter;
GRANT REFERENCES, SELECT ON customers TO hr;
COMMIT;

ALTER SESSION SET CURRENT_SCHEMA = hr;
CREATE TABLE departments (id NUMBER PRIMARY KEY, name VARCHAR2(50) NOT NULL);
CREATE TABLE account_managers (
  dept_id     NUMBER NOT NULL REFERENCES departments (id),
  customer_id NUMBER NOT NULL CONSTRAINT fk_am_customer REFERENCES shop.customers (id)
);
INSERT INTO departments VALUES (1, 'R&D');
INSERT INTO account_managers SELECT 1, id FROM shop.customers WHERE email = 'ada@example.com';
COMMIT;
