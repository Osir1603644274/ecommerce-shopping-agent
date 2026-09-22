package com.example.locallife.support;

import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;

/** Runs the same business assertions on real migrations and an explicitly isolated MySQL database. */
@EnabledIfEnvironmentVariable(named="SUPPORT_MYSQL_URL",matches="jdbc:mysql:.*")
class SupportMysqlAcceptanceIT extends SupportServiceIntegrationTests {
    @DynamicPropertySource
    static void mysql(DynamicPropertyRegistry properties) {
        properties.add("spring.datasource.url",()->System.getenv("SUPPORT_MYSQL_URL"));
        properties.add("spring.datasource.username",()->System.getenv("SUPPORT_MYSQL_USER"));
        properties.add("spring.datasource.password",()->System.getenv("SUPPORT_MYSQL_PASSWORD"));
        properties.add("spring.datasource.driver-class-name",()->"com.mysql.cj.jdbc.Driver");
        properties.add("spring.sql.init.mode",()->"never");
        properties.add("spring.flyway.enabled",()->"true");
    }
}
