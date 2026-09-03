package com.example.locallife.memory;

import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.MySQLContainer;

import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;

class ShoppingMemoryV13MigrationIT {
    @Test void v13AddsCatalogContractWithoutChangingLegacyV2Rows() throws Exception {
        try (MySQLContainer<?> mysql = new MySQLContainer<>("mysql:8.4")
                .withDatabaseName("memory_v13")
                .withUsername("test")
                .withPassword("test")) {
            mysql.start();
            Flyway.configure()
                    .dataSource(mysql.getJdbcUrl(), mysql.getUsername(), mysql.getPassword())
                    .target("12").load().migrate();
            String owner = UUID.randomUUID().toString();
            try (var connection = java.sql.DriverManager.getConnection(
                    mysql.getJdbcUrl(), mysql.getUsername(), mysql.getPassword());
                 var statement = connection.createStatement()) {
                statement.executeUpdate("INSERT INTO user_account(id,username,password_hash,enabled,token_version) VALUES('"
                        + owner + "','v13-" + owner.substring(0, 8) + "','x',TRUE,0)");
                statement.executeUpdate("INSERT INTO user_shopping_memory("
                        + "id,owner_user_id,category,memory_category,product_category,recipient_scope,source,data_class,"
                        + "semantic_key,token_value,version,status,expires_at,command_digest,content_digest) VALUES("
                        + "'legacy-row','" + owner + "','shopping_preference','shopping_preference','phone','self',"
                        + "'explicit_user','long_term_preference','avoid_brand','apple',1,'ACTIVE','2030-01-01','"
                        + "a".repeat(64) + "','" + "b".repeat(64) + "')");

                Flyway.configure()
                        .dataSource(mysql.getJdbcUrl(), mysql.getUsername(), mysql.getPassword())
                        .target("13").load().migrate();

                var legacy = statement.executeQuery(
                        "SELECT schema_version,preference_kind,catalog_revision FROM user_shopping_memory WHERE id='legacy-row'"
                );
                legacy.next();
                assertThat(legacy.getInt(1)).isEqualTo(2);
                assertThat(legacy.getString(2)).isEqualTo("avoid");
                assertThat(legacy.getString(3)).isEqualTo("legacy-v12");
                statement.executeUpdate("INSERT INTO shopping_memory_catalog_value("
                        + "catalog_revision,category_id,attribute_key,normalized_value,display_label) VALUES("
                        + "'shopping-companion-v1','exercise-fitness','equipment_type','indoor-bike','Indoor Bike')");
                var catalog = statement.executeQuery(
                        "SELECT COUNT(*) FROM shopping_memory_catalog_value WHERE active=TRUE"
                );
                catalog.next();
                assertThat(catalog.getInt(1)).isEqualTo(1);
            }
        }
    }
}
