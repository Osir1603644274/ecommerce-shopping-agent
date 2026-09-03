package com.example.locallife.memory;

import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.MySQLContainer;

import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;

class ShoppingMemoryV11MigrationIT {
    @Test void legacySensitiveRowsAreAuditedAndV2ScopeIsNeverInvented() throws Exception {
        try (MySQLContainer<?> mysql = new MySQLContainer<>("mysql:8.4")
                .withDatabaseName("memory_v11").withUsername("test").withPassword("test")) {
            mysql.start();
            Flyway.configure().dataSource(mysql.getJdbcUrl(),mysql.getUsername(),mysql.getPassword()).target("10").load().migrate();
            try (var connection=java.sql.DriverManager.getConnection(mysql.getJdbcUrl(),mysql.getUsername(),mysql.getPassword());
                 var statement=connection.createStatement()) {
                String owner=UUID.randomUUID().toString();
                String sensitive=UUID.randomUUID().toString();
                String shopping=UUID.randomUUID().toString();
                statement.executeUpdate("INSERT INTO user_account(id,username,password_hash,enabled,token_version) VALUES('"+owner+"','v11-"+owner.substring(0,8)+"','x',TRUE,0)");
                statement.executeUpdate("INSERT INTO user_shopping_memory(id,owner_user_id,category,semantic_key,token_value,version,status,expires_at,command_digest,content_digest) VALUES('"+sensitive+"','"+owner+"','health','avoid_material','latex',1,'ACTIVE','2030-01-01','"+"a".repeat(64)+"','"+"b".repeat(64)+"')");
                statement.executeUpdate("INSERT INTO user_shopping_memory(id,owner_user_id,category,semantic_key,token_value,version,status,expires_at,command_digest,content_digest) VALUES('"+shopping+"','"+owner+"','shopping_preference','avoid_brand','brand-x',1,'ACTIVE','2030-01-01','"+"c".repeat(64)+"','"+"d".repeat(64)+"')");
                Flyway.configure().dataSource(mysql.getJdbcUrl(),mysql.getUsername(),mysql.getPassword()).load().migrate();

                var result=statement.executeQuery("SELECT status,product_category,recipient_scope,source,data_class FROM user_shopping_memory WHERE id='"+sensitive+"'");
                result.next();
                assertThat(result.getString("status")).isEqualTo("LEGACY_REJECTED");
                assertThat(result.getString("product_category")).isEqualTo("unknown");
                assertThat(result.getString("recipient_scope")).isEqualTo("unknown");
                assertThat(result.getString("source")).isEqualTo("legacy_unverified");
                assertThat(result.getString("data_class")).isEqualTo("long_term_preference");
                result=statement.executeQuery("SELECT COUNT(*) FROM user_memory_audit WHERE memory_id='"+sensitive+"' AND operation='MIGRATE_V11' AND outcome='SUPPRESSED'");
                result.next(); assertThat(result.getInt(1)).isEqualTo(1);
                result=statement.executeQuery("SELECT COUNT(*) FROM user_shopping_memory WHERE id='"+shopping+"' AND product_category='unknown' AND recipient_scope='unknown' AND source='legacy_unverified'");
                result.next(); assertThat(result.getInt(1)).isEqualTo(1);
            }
        }
    }
}
