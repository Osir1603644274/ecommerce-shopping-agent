package com.example.locallife.memory;

import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.MySQLContainer;

import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;

/** Proves the forward V12 chain key includes product and recipient scope. */
class ShoppingMemoryV12MigrationIT {
    @Test void v12AllowsIndependentProductScopedChainsAndCreatesWriteAuthorityTables() throws Exception {
        try (MySQLContainer<?> mysql = new MySQLContainer<>("mysql:8.4")
                .withDatabaseName("memory_v12").withUsername("test").withPassword("test")) {
            mysql.start();
            Flyway.configure().dataSource(mysql.getJdbcUrl(), mysql.getUsername(), mysql.getPassword())
                    .target("11").load().migrate();
            try (var connection = java.sql.DriverManager.getConnection(
                    mysql.getJdbcUrl(), mysql.getUsername(), mysql.getPassword());
                 var statement = connection.createStatement()) {
                String owner = UUID.randomUUID().toString();
                statement.executeUpdate("INSERT INTO user_account(id,username,password_hash,enabled,token_version) VALUES('"
                        + owner + "','v12-" + owner.substring(0, 8) + "','x',TRUE,0)");

                Flyway.configure().dataSource(mysql.getJdbcUrl(), mysql.getUsername(), mysql.getPassword())
                        .load().migrate();
                String digestA = "a".repeat(64);
                String digestB = "b".repeat(64);
                insertV2Memory(statement, "phone-row", owner, "phone", digestA, digestB);
                insertV2Memory(statement, "laptop-row", owner, "laptop", digestB, digestA);

                var rows = statement.executeQuery("SELECT COUNT(*) FROM user_shopping_memory WHERE owner_user_id='"
                        + owner + "' AND semantic_key='os' AND version=1");
                rows.next();
                assertThat(rows.getInt(1)).isEqualTo(2);
                var tables = statement.executeQuery("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name IN ('user_memory_consent_grant','user_memory_command_result')");
                tables.next();
                assertThat(tables.getInt(1)).isEqualTo(2);
            }
        }
    }

    private static void insertV2Memory(
            java.sql.Statement statement, String id, String owner, String productCategory,
            String commandDigest, String contentDigest
    ) throws Exception {
        statement.executeUpdate("INSERT INTO user_shopping_memory("
                + "id,owner_user_id,category,memory_category,product_category,recipient_scope,source,data_class,"
                + "semantic_key,token_value,version,status,expires_at,command_digest,content_digest"
                + ") VALUES('" + id + "','" + owner
                + "','shopping_preference','shopping_preference','" + productCategory
                + "','self','explicit_user','long_term_preference','os','android',1,'ACTIVE','2030-01-01','"
                + commandDigest + "','" + contentDigest + "')");
    }
}
