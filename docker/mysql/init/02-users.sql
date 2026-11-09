-- Users for testing the Schema Dump permission check.
-- reporter: SELECT on one table only -> cannot dump shop.
CREATE USER 'reporter'@'%' IDENTIFIED BY 'reporter';
GRANT SELECT ON shop.products TO 'reporter'@'%';
-- dumper: SELECT + SHOW VIEW on shop, but no access to routines -> dump works, routines skipped.
CREATE USER 'dumper'@'%' IDENTIFIED BY 'dumper';
GRANT SELECT, SHOW VIEW ON shop.* TO 'dumper'@'%';
