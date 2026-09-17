package com.example.locallife.product;

import com.example.locallife.common.ApiResponse;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.Gauge;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import jakarta.servlet.*;
import jakarta.servlet.http.*;
import java.io.IOException;
import java.sql.SQLTimeoutException;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import java.util.Optional;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.core.annotation.Order;
import org.springframework.dao.QueryTimeoutException;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

/** Per-instance admission, not a separate datasource and not a global DB capacity guarantee. */
@Component
@Order(-90) // After Spring Security (-100); unauthorized requests do not consume permits.
@ConditionalOnProperty(name="local-life.catalog-read.enabled", havingValue="true", matchIfMissing=true)
public class CatalogReadAdmissionFilter extends OncePerRequestFilter {
    private final Semaphore slots;
    private final int sqlSeconds;
    private final int waitMillis;
    private final boolean admissionEnabled;
    private final ObjectMapper json;
    private final Counter rejected;
    private final Counter timedOut;
    public CatalogReadAdmissionFilter(ObjectMapper json, Optional<MeterRegistry> registry,
            @Value("${local-life.catalog-read.max-concurrent:0}") int concurrent,
            @Value("${local-life.catalog-read.sql-timeout-seconds:2}") int sqlSeconds,
            @Value("${local-life.catalog-read.max-wait-millis:1000}") int waitMillis,
            @Value("${spring.datasource.hikari.maximum-pool-size:10}") int poolSize) {
        if (concurrent < 0 || concurrent > poolSize - 2 || sqlSeconds < 1 || waitMillis < 0 || waitMillis > 1000)
            throw new IllegalArgumentException("Catalog reads must leave at least two pool slots; positive SQL timeout required");
        this.slots = new Semaphore(concurrent);
        this.admissionEnabled = concurrent > 0;
        this.sqlSeconds = sqlSeconds;
        this.waitMillis = waitMillis;
        this.json = json;
        MeterRegistry metrics = registry.orElseGet(SimpleMeterRegistry::new);
        rejected = metrics.counter("catalog.read.rejected");
        timedOut = metrics.counter("catalog.read.sql.timeout");
        Gauge.builder("catalog.read.active", slots, s -> concurrent - s.availablePermits()).register(metrics);
    }
    @Override protected boolean shouldNotFilter(HttpServletRequest request) {
        String path = request.getRequestURI().substring(request.getContextPath().length());
        boolean productRead = "GET".equals(request.getMethod())
                && (path.equals("/api/products") || path.startsWith("/api/products/"));
        boolean resolve = "POST".equals(request.getMethod()) && path.equals("/api/products/resolve");
        return !(productRead || resolve);
    }
    @Override protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response,
            FilterChain chain) throws ServletException, IOException {
        if (!admissionEnabled) {
            invokeWithBudget(request, response, chain);
            return;
        }
        boolean admitted;
        try { admitted = slots.tryAcquire(waitMillis, TimeUnit.MILLISECONDS); }
        catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
            rejected.increment();
            busy(response, "商品查询已中断，请稍后重试");
            return;
        }
        if (!admitted) {
            rejected.increment();
            busy(response, "商品查询繁忙，请稍后重试");
            return;
        }
        try { invokeWithBudget(request, response, chain); }
        finally { slots.release(); }
    }
    private void invokeWithBudget(HttpServletRequest request, HttpServletResponse response,
            FilterChain chain) throws ServletException, IOException {
        try (var ignored = CatalogReadBudget.open(sqlSeconds)) {
            try { chain.doFilter(request, response); }
            catch (ServletException | RuntimeException error) {
                if (!isSqlTimeout(error) || response.isCommitted()) throw error;
                timedOut.increment();
                busy(response, "商品查询超时，请稍后重试");
            }
        }
    }
    private static boolean isSqlTimeout(Throwable error) {
        for (int depth = 0; error != null && depth < 20; depth++, error = error.getCause())
            if (error instanceof SQLTimeoutException || error instanceof QueryTimeoutException) return true;
        return false;
    }
    private void busy(HttpServletResponse response, String message) throws IOException {
        response.setStatus(503);
        response.setHeader("Retry-After", "1");
        response.setContentType("application/json");
        response.setCharacterEncoding("UTF-8");
        json.writeValue(response.getWriter(), ApiResponse.fail(message));
    }
}
