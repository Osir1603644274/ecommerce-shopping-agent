package com.example.locallife.product;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import java.util.Optional;
import java.util.List;
import java.util.Set;
import java.util.HashSet;
import java.util.Collections;

/** No inferred price is ever promoted to a verified source snapshot. */
@Service
public class LocalOfferService {
    private final JdbcTemplate jdbc;
    private final boolean enabled;
    public LocalOfferService(JdbcTemplate jdbc, @Value("${local-life.demo-commerce-enabled:false}") boolean enabled) {
        this.jdbc=jdbc; this.enabled=enabled;
    }
    public Optional<Offer> find(long id) {
        if (!enabled) return Optional.empty();
        return CatalogReadBudget.query(jdbc,"SELECT price_minor,currency,price_kind,version FROM product_local_offer WHERE product_id=?",
            (rs,n)->new Offer(rs.getLong(1),rs.getString(2),rs.getString(3),rs.getLong(4)),id).stream().findFirst();
    }
    public Set<Long> findProductIds(List<Long> ids) {
        if (!enabled || ids.isEmpty()) return Set.of();
        List<Long> unique = ids.stream().distinct().toList();
        Set<Long> result = new HashSet<>();
        for (int start = 0; start < unique.size(); start += 500) {
            List<Long> chunk = unique.subList(start, Math.min(start + 500, unique.size()));
            String sql = "SELECT product_id FROM product_local_offer WHERE product_id IN ("
                    + String.join(",", Collections.nCopies(chunk.size(), "?")) + ")";
            result.addAll(jdbc.query(sql, statement -> {
                for (int i = 0; i < chunk.size(); i++) statement.setLong(i + 1, chunk.get(i));
                CatalogReadBudget.apply(statement);
            }, (rs, n) -> rs.getLong(1)));
        }
        return result;
    }
    public record Offer(long priceMinor, String currency, String kind, long version) { }
}
