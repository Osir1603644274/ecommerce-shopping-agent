package com.example.locallife.inventory.remote;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import jakarta.annotation.PreDestroy;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.boot.autoconfigure.jdbc.DataSourceProperties;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.stereotype.Component;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.support.TransactionTemplate;
import java.util.function.Consumer;

/** Tiny independent writer pool avoids REQUIRES_NEW starving behind order connections. */
@Component
@ConditionalOnProperty(name="local-life.inventory.remote-enabled",havingValue="true")
public class InventoryIntentStore {
    private final HikariDataSource pool;private final JdbcTemplate jdbc;private final TransactionTemplate tx;
    public InventoryIntentStore(DataSourceProperties properties){
        var config=new HikariConfig();config.setJdbcUrl(properties.determineUrl());
        config.setUsername(properties.determineUsername());config.setPassword(properties.determinePassword());
        config.setPoolName("inventory-intent-writer");config.setMaximumPoolSize(2);config.setMinimumIdle(0);
        config.setConnectionTimeout(1000);config.setInitializationFailTimeout(-1);
        config.setConnectionInitSql("SET SESSION innodb_lock_wait_timeout=3");
        pool=new HikariDataSource(config);jdbc=new JdbcTemplate(pool);jdbc.setQueryTimeout(3);
        tx=new TransactionTemplate(new DataSourceTransactionManager(pool));
        tx.setPropagationBehavior(TransactionDefinition.PROPAGATION_REQUIRES_NEW);tx.setTimeout(4);
    }
    public void write(Consumer<JdbcTemplate> action){tx.executeWithoutResult(status->action.accept(jdbc));}
    @PreDestroy public void close(){pool.close();}
}
