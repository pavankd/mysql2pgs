-- Sample data exercising MySQL 5.6 features the migrator has to translate.
SET NAMES utf8mb4;
SET SESSION sql_mode = 'NO_ENGINE_SUBSTITUTION';

CREATE DATABASE IF NOT EXISTS shop DEFAULT CHARACTER SET utf8mb4;
USE shop;

CREATE TABLE Customers (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT,
  email VARCHAR(191) NOT NULL,
  full_name VARCHAR(120) NOT NULL,
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  flags BIT(4) NOT NULL DEFAULT b'0101',
  bio TEXT,
  birth_date DATE NULL DEFAULT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT '0000-00-00 00:00:00' ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_email (email),
  KEY ix_name_prefix (full_name(20)),
  FULLTEXT KEY ft_bio (bio)
) ENGINE=MyISAM COMMENT='Registered customers';

CREATE TABLE products (
  id INT NOT NULL AUTO_INCREMENT,
  sku CHAR(12) NOT NULL,
  title VARCHAR(255) NOT NULL COMMENT 'Display name',
  price DECIMAL(10,2) NOT NULL DEFAULT '0.00',
  weight_kg FLOAT DEFAULT NULL,
  rating DOUBLE DEFAULT NULL,
  stock MEDIUMINT UNSIGNED NOT NULL DEFAULT 0,
  status ENUM('draft','active','it''s retired') NOT NULL DEFAULT 'draft',
  tags SET('new','sale','gift') DEFAULT NULL,
  thumbnail BLOB,
  release_year YEAR DEFAULT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_sku (sku)
) ENGINE=InnoDB;

CREATE TABLE orders (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  customer_id INT UNSIGNED NOT NULL,
  placed_at DATETIME NOT NULL,
  ship_window TIME DEFAULT NULL,
  note VARCHAR(500) DEFAULT NULL,
  PRIMARY KEY (id),
  KEY ix_customer (customer_id)
) ENGINE=InnoDB;

CREATE TABLE order_items (
  order_id BIGINT UNSIGNED NOT NULL,
  product_id INT NOT NULL,
  qty SMALLINT UNSIGNED NOT NULL DEFAULT 1,
  unit_price DECIMAL(10,2) NOT NULL,
  PRIMARY KEY (order_id, product_id),
  KEY ix_product (product_id),
  CONSTRAINT fk_items_order FOREIGN KEY (order_id) REFERENCES orders (id) ON DELETE CASCADE,
  CONSTRAINT fk_items_product FOREIGN KEY (product_id) REFERENCES products (id)
) ENGINE=InnoDB;


INSERT INTO Customers (email, full_name, is_active, flags, bio, birth_date, created_at, updated_at) VALUES
  ('ada@example.com', 'Ada Lovelace', 1, b'1010', 'Mathematician and first programmer', '1815-12-10', '2024-01-02 03:04:05', '2024-01-02 03:04:05'),
  ('alan@example.com', 'Alan Turing', 1, b'0011', 'Codebreaker. Emoji test: 🚀', '1912-06-23', '2024-02-02 10:00:00', '0000-00-00 00:00:00'),
  ('grace@example.com', 'Grace Hopper', 0, b'0000', NULL, '0000-00-00', '2024-03-03 12:30:00', '2024-03-04 08:00:00');

INSERT INTO products (sku, title, price, weight_kg, rating, stock, status, tags, thumbnail, release_year) VALUES
  ('SKU-0001', 'Mechanical keyboard', 129.99, 1.2, 4.7, 40, 'active', 'new,sale', UNHEX('89504E47'), 2023),
  ('SKU-0002', 'Ergonomic mouse', 59.50, 0.15, 4.2, 0, 'it''s retired', NULL, NULL, 2019),
  ('SKU-0003', 'USB-C hub', 34.00, 0.08, NULL, 120, 'draft', 'gift', NULL, NULL);

INSERT INTO orders (customer_id, placed_at, ship_window, note) VALUES
  (1, '2024-05-01 09:15:00', '08:00:00', 'Leave at door'),
  (2, '2024-05-02 18:45:30', NULL, 'Quote''s "test" \\ backslash'),
  (1, '2024-05-03 07:00:00', '23:59:59', NULL);

INSERT INTO order_items VALUES (1, 1, 1, 129.99), (1, 3, 2, 34.00), (2, 2, 1, 59.50), (3, 3, 5, 30.00);

-- Bulk rows to make progress visible
CREATE TABLE events (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  kind VARCHAR(32) NOT NULL,
  payload VARCHAR(255),
  happened_at DATETIME(3) NOT NULL
) ENGINE=InnoDB;

INSERT INTO events (kind, payload, happened_at) VALUES ('seed', 'x', '2024-01-01 00:00:00.123');
INSERT INTO events (kind, payload, happened_at) SELECT 'seed', 'x', NOW(3) FROM events;
INSERT INTO events (kind, payload, happened_at) SELECT 'seed', 'x', NOW(3) FROM events;
INSERT INTO events (kind, payload, happened_at) SELECT 'seed', 'x', NOW(3) FROM events;
INSERT INTO events (kind, payload, happened_at) SELECT 'seed', 'x', NOW(3) FROM events;
INSERT INTO events (kind, payload, happened_at) SELECT 'seed', 'x', NOW(3) FROM events;
INSERT INTO events (kind, payload, happened_at) SELECT 'seed', 'x', NOW(3) FROM events;
INSERT INTO events (kind, payload, happened_at) SELECT 'seed', 'x', NOW(3) FROM events;
INSERT INTO events (kind, payload, happened_at) SELECT 'seed', 'x', NOW(3) FROM events;
INSERT INTO events (kind, payload, happened_at) SELECT 'seed', 'x', NOW(3) FROM events;
INSERT INTO events (kind, payload, happened_at) SELECT 'seed', 'x', NOW(3) FROM events;
INSERT INTO events (kind, payload, happened_at) SELECT 'seed', 'x', NOW(3) FROM events;
INSERT INTO events (kind, payload, happened_at) SELECT 'seed', 'x', NOW(3) FROM events;
INSERT INTO events (kind, payload, happened_at) SELECT 'seed', 'x', NOW(3) FROM events;
INSERT INTO events (kind, payload, happened_at) SELECT 'seed', 'x', NOW(3) FROM events;
INSERT INTO events (kind, payload, happened_at) SELECT 'seed', 'x', NOW(3) FROM events;
INSERT INTO events (kind, payload, happened_at) SELECT 'seed', 'x', NOW(3) FROM events;
INSERT INTO events (kind, payload, happened_at) SELECT 'seed', 'x', NOW(3) FROM events;
INSERT INTO events (kind, payload, happened_at) SELECT 'seed', 'x', NOW(3) FROM events;

CREATE VIEW order_totals AS
  SELECT o.id AS order_id, c.email, SUM(i.qty * i.unit_price) AS total, IFNULL(o.note, '') AS note
  FROM orders o
  JOIN Customers c ON c.id = o.customer_id
  JOIN order_items i ON i.order_id = o.id
  GROUP BY o.id, c.email, o.note;

DELIMITER //
CREATE PROCEDURE top_products() BEGIN SELECT * FROM products ORDER BY rating DESC; END //
DELIMITER ;

CREATE DATABASE IF NOT EXISTS blog DEFAULT CHARACTER SET utf8mb4;
USE blog;
CREATE TABLE posts (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  slug VARCHAR(191) NOT NULL UNIQUE,
  body LONGTEXT,
  published TINYINT(1) DEFAULT 0
) ENGINE=InnoDB;
INSERT INTO posts (slug, body, published) VALUES ('hello-world', 'First post', 1), ('draft', NULL, 0);
