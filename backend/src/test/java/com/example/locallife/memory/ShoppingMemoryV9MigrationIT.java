package com.example.locallife.memory;

import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.MySQLContainer;
import java.util.UUID;
import static org.assertj.core.api.Assertions.assertThat;

class ShoppingMemoryV9MigrationIT {
    @Test void upgradesV8RowsIntoAuthoritativeProjectionHeads() throws Exception {
        try (MySQLContainer<?> mysql = new MySQLContainer<>("mysql:8.4").withDatabaseName("memory_upgrade").withUsername("test").withPassword("test")) {
            mysql.start();
            Flyway.configure().dataSource(mysql.getJdbcUrl(),mysql.getUsername(),mysql.getPassword()).target("8").load().migrate();
            try (var c=java.sql.DriverManager.getConnection(mysql.getJdbcUrl(),mysql.getUsername(),mysql.getPassword()); var s=c.createStatement()) {
                String owner=UUID.randomUUID().toString();
                String firstEntryId=UUID.randomUUID().toString();
                s.executeUpdate("INSERT INTO user_account(id,username,password_hash,enabled,token_version) VALUES('"+owner+"','upgrade-"+owner.substring(0,8)+"','x',TRUE,0)");
                s.executeUpdate("INSERT INTO user_shopping_memory(id,owner_user_id,category,semantic_key,token_value,version,status,expires_at,command_digest,content_digest) VALUES('"+firstEntryId+"','"+owner+"','shopping_preference','avoid_brand','brand-x',1,'SUPERSEDED','2030-01-01','"+"a".repeat(64)+"','"+"b".repeat(64)+"')");
                s.executeUpdate("INSERT INTO user_shopping_memory(id,owner_user_id,category,semantic_key,token_value,version,status,expires_at,supersedes_id,command_digest,content_digest) VALUES('"+UUID.randomUUID()+"','"+owner+"','shopping_preference','avoid_brand','brand-a',2,'ACTIVE','2030-01-01','"+firstEntryId+"','"+"c".repeat(64)+"','"+"d".repeat(64)+"')");
                Flyway.configure().dataSource(mysql.getJdbcUrl(),mysql.getUsername(),mysql.getPassword()).load().migrate();
                var rs=s.executeQuery("SELECT revision FROM user_memory_projection_head WHERE owner_user_id='"+owner+"'"); rs.next(); assertThat(rs.getLong(1)).isEqualTo(2L);
                rs=s.executeQuery("SELECT COUNT(*) FROM user_shopping_memory WHERE owner_user_id='"+owner+"' AND status='ACTIVE' AND token_value='brand-a'");rs.next();assertThat(rs.getInt(1)).isEqualTo(1);
            }
        }
    }
}
