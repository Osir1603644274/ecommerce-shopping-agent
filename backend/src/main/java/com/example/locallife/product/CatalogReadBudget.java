package com.example.locallife.product;

import java.sql.SQLException;
import java.sql.Statement;
import java.util.List;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;

/** Request-scoped SQL limit; never leaks into the next request on a servlet thread. */
public final class CatalogReadBudget implements AutoCloseable {
    private static final ThreadLocal<Integer> SECONDS = new ThreadLocal<>();
    private final Integer previous;
    private CatalogReadBudget(int seconds) {
        previous = SECONDS.get();
        SECONDS.set(seconds);
    }
    public static CatalogReadBudget open(int seconds) {
        if (seconds < 1) throw new IllegalArgumentException("SQL timeout must be positive");
        return new CatalogReadBudget(seconds);
    }
    public static void apply(Statement statement) throws SQLException {
        Integer seconds = SECONDS.get();
        if (seconds == null) return;
        int existing = statement.getQueryTimeout();
        statement.setQueryTimeout(existing > 0 ? Math.min(existing, seconds) : seconds);
    }
    public static <T> List<T> query(JdbcTemplate jdbc, String sql, RowMapper<T> mapper, long id) {
        if (SECONDS.get() == null) return jdbc.query(sql, mapper, id);
        return jdbc.query(sql, statement -> {
            statement.setLong(1, id);
            apply(statement);
        }, mapper);
    }
    @Override public void close() {
        if (previous == null) SECONDS.remove(); else SECONDS.set(previous);
    }
}
