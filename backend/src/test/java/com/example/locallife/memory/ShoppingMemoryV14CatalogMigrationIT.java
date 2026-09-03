package com.example.locallife.memory;

import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.MySQLContainer;

import static org.assertj.core.api.Assertions.assertThat;

class ShoppingMemoryV14CatalogMigrationIT {
    @Test void v14SeedsOnlyThePinnedUsedPhoneCatalog() throws Exception {
        try (MySQLContainer<?> mysql = new MySQLContainer<>("mysql:8.4")
                .withDatabaseName("memory_v14")
                .withUsername("test")
                .withPassword("test")) {
            mysql.start();
            Flyway.configure()
                    .dataSource(mysql.getJdbcUrl(), mysql.getUsername(), mysql.getPassword())
                    .load().migrate();
            try (var connection = java.sql.DriverManager.getConnection(
                    mysql.getJdbcUrl(), mysql.getUsername(), mysql.getPassword());
                 var statement = connection.createStatement();
                 var rows = statement.executeQuery(
                         "SELECT COUNT(*), COUNT(DISTINCT category_id), "
                                 + "COUNT(DISTINCT catalog_revision) "
                                 + "FROM shopping_memory_catalog_value WHERE active=TRUE")) {
                rows.next();
                assertThat(rows.getInt(1)).isEqualTo(29);
                assertThat(rows.getInt(2)).isEqualTo(1);
                assertThat(rows.getInt(3)).isEqualTo(1);
            }
        }
    }
}
