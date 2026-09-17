package com.example.locallife.search;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;
import java.util.UUID;

/** Joined to the mutation transaction; the relay sees only committed invalidations. */
@Component
public class CacheInvalidationPublisher implements com.example.locallife.integration.CacheInvalidationRequests {
    static final String CHANNEL = "local-life.cache-invalidation.v1";
    private final JdbcTemplate jdbc;
    public CacheInvalidationPublisher(JdbcTemplate jdbc) { this.jdbc=jdbc; }
    @Transactional public void shopUpdated(Long id) { append("shop",id); }
    @Transactional public void productUpdated(Long id) { append("product",id); }
    private void append(String entity, Long id) {
        jdbc.update("INSERT INTO cache_invalidation_outbox(id,entity_type,entity_id) VALUES(?,?,?)",
                UUID.randomUUID().toString(),entity,id);
    }
}
