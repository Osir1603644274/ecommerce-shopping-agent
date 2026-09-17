#!/usr/bin/env bash
set -euo pipefail

case "${MYSQL_DATABASE:?}" in
  (*[!A-Za-z0-9_]*) echo "MYSQL_DATABASE contains unsupported characters" >&2; exit 2 ;;
esac

: "${MYSQL_ROOT_PASSWORD:?}"
: "${CATALOG_DB_PASSWORD:?}"
: "${TRADE_DB_PASSWORD:?}"

export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"
mysql --protocol=TCP --host=mysql --user=root <<SQL
CREATE USER IF NOT EXISTS 'catalog_service'@'%' IDENTIFIED BY '${CATALOG_DB_PASSWORD}';
ALTER USER 'catalog_service'@'%' IDENTIFIED BY '${CATALOG_DB_PASSWORD}';
CREATE USER IF NOT EXISTS 'trade_service'@'%' IDENTIFIED BY '${TRADE_DB_PASSWORD}';
ALTER USER 'trade_service'@'%' IDENTIFIED BY '${TRADE_DB_PASSWORD}';

REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'catalog_service'@'%';
REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'trade_service'@'%';

GRANT SELECT, INSERT, UPDATE ON ${MYSQL_DATABASE}.product TO 'catalog_service'@'%';
GRANT SELECT ON ${MYSQL_DATABASE}.product_attribute TO 'catalog_service'@'%';
GRANT SELECT, INSERT, UPDATE ON ${MYSQL_DATABASE}.shop TO 'catalog_service'@'%';
GRANT SELECT ON ${MYSQL_DATABASE}.shop_type TO 'catalog_service'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON ${MYSQL_DATABASE}.review TO 'catalog_service'@'%';
GRANT SELECT, INSERT, UPDATE ON ${MYSQL_DATABASE}.review_projection_head TO 'catalog_service'@'%';
GRANT SELECT, INSERT ON ${MYSQL_DATABASE}.user_behavior TO 'catalog_service'@'%';
GRANT SELECT, INSERT, UPDATE ON ${MYSQL_DATABASE}.catalog_state TO 'catalog_service'@'%';
GRANT SELECT, INSERT, UPDATE ON ${MYSQL_DATABASE}.product_search_projection_cursor TO 'catalog_service'@'%';

GRANT SELECT, INSERT, UPDATE ON ${MYSQL_DATABASE}.auth_session_audit TO 'trade_service'@'%';
GRANT SELECT, INSERT, UPDATE ON ${MYSQL_DATABASE}.coupon_template TO 'trade_service'@'%';
GRANT SELECT, INSERT, UPDATE ON ${MYSQL_DATABASE}.customer_order TO 'trade_service'@'%';
GRANT SELECT, INSERT, UPDATE ON ${MYSQL_DATABASE}.flash_sale_campaign TO 'trade_service'@'%';
GRANT SELECT, INSERT, UPDATE ON ${MYSQL_DATABASE}.flash_sale_order TO 'trade_service'@'%';
GRANT SELECT, INSERT, UPDATE ON ${MYSQL_DATABASE}.flash_sale_request TO 'trade_service'@'%';
GRANT SELECT, INSERT, UPDATE ON ${MYSQL_DATABASE}.inventory_reservation TO 'trade_service'@'%';
GRANT SELECT, INSERT, UPDATE ON ${MYSQL_DATABASE}.inventory_stock TO 'trade_service'@'%';
GRANT SELECT, INSERT, UPDATE ON ${MYSQL_DATABASE}.order_item TO 'trade_service'@'%';
GRANT SELECT, INSERT, UPDATE ON ${MYSQL_DATABASE}.payment_notification TO 'trade_service'@'%';
GRANT SELECT, INSERT, UPDATE ON ${MYSQL_DATABASE}.payment_record TO 'trade_service'@'%';
GRANT SELECT, INSERT, UPDATE ON ${MYSQL_DATABASE}.refund_record TO 'trade_service'@'%';
GRANT SELECT ON ${MYSQL_DATABASE}.shopping_memory_catalog_value TO 'trade_service'@'%';
GRANT SELECT, INSERT, UPDATE ON ${MYSQL_DATABASE}.user_account TO 'trade_service'@'%';
GRANT SELECT, INSERT, UPDATE ON ${MYSQL_DATABASE}.user_coupon TO 'trade_service'@'%';
GRANT SELECT, INSERT ON ${MYSQL_DATABASE}.user_memory_audit TO 'trade_service'@'%';
GRANT SELECT, INSERT, UPDATE ON ${MYSQL_DATABASE}.user_memory_command_result TO 'trade_service'@'%';
GRANT SELECT, INSERT, UPDATE ON ${MYSQL_DATABASE}.user_memory_consent_consumption TO 'trade_service'@'%';
GRANT SELECT, INSERT, UPDATE ON ${MYSQL_DATABASE}.user_memory_consent_grant TO 'trade_service'@'%';
GRANT SELECT, INSERT, UPDATE ON ${MYSQL_DATABASE}.user_memory_projection_head TO 'trade_service'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON ${MYSQL_DATABASE}.user_role TO 'trade_service'@'%';
GRANT SELECT, INSERT, UPDATE ON ${MYSQL_DATABASE}.user_shopping_memory TO 'trade_service'@'%';

GRANT SELECT, INSERT, UPDATE ON ${MYSQL_DATABASE}.outbox_event TO 'catalog_service'@'%', 'trade_service'@'%';
GRANT SELECT, INSERT, UPDATE ON ${MYSQL_DATABASE}.inbox_event TO 'catalog_service'@'%', 'trade_service'@'%';
GRANT SELECT, INSERT, UPDATE ON ${MYSQL_DATABASE}.dead_letter_event TO 'catalog_service'@'%', 'trade_service'@'%';
FLUSH PRIVILEGES;
GRANT SELECT, INSERT, UPDATE ON ${MYSQL_DATABASE}.cache_invalidation_outbox TO 'catalog_service'@'%', 'trade_service'@'%';
GRANT SELECT, INSERT, UPDATE ON ${MYSQL_DATABASE}.flash_sale_request TO 'trade_service'@'%';
SQL
