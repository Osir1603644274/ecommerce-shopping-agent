package com.example.locallife.product;

import com.fasterxml.jackson.databind.ObjectMapper;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import jakarta.servlet.ServletException;
import java.sql.Statement;
import java.sql.SQLTimeoutException;
import java.util.Optional;
import java.util.concurrent.*;
import org.junit.jupiter.api.Test;
import org.springframework.mock.web.*;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

class CatalogReadProtectionTests {
    CatalogReadAdmissionFilter filter(SimpleMeterRegistry metrics) {
        return new CatalogReadAdmissionFilter(new ObjectMapper().findAndRegisterModules(), Optional.of(metrics), 1, 2, 50, 3);
    }
    @Test void rejectsExcessReadsButDoesNotBlockOrdersAndReleasesSlot() throws Exception {
        var metrics = new SimpleMeterRegistry(); var filter = filter(metrics);
        var entered = new CountDownLatch(1); var release = new CountDownLatch(1);
        var executor = Executors.newSingleThreadExecutor();
        try {
            var first = executor.submit(() -> {
                filter.doFilter(new MockHttpServletRequest("GET", "/api/products/1"), new MockHttpServletResponse(), (a,b) -> {
                    entered.countDown();
                    try { assertTrue(release.await(3, TimeUnit.SECONDS)); }
                    catch (InterruptedException e) { Thread.currentThread().interrupt(); throw new ServletException(e); }
                }); return null;
            });
            assertTrue(entered.await(2, TimeUnit.SECONDS));
            var blocked = new MockHttpServletResponse();
            filter.doFilter(new MockHttpServletRequest("GET", "/api/products/2"), blocked, (a,b) -> fail("must not enter DB chain"));
            assertEquals(503, blocked.getStatus()); assertEquals("1", blocked.getHeader("Retry-After"));
            assertTrue(blocked.getContentAsString().contains("商品查询繁忙"));
            filter.doFilter(new MockHttpServletRequest("POST", "/api/orders"), new MockHttpServletResponse(), (a,b) -> {});
            assertEquals(1, metrics.counter("catalog.read.rejected").count());
            release.countDown(); first.get(3, TimeUnit.SECONDS);
            filter.doFilter(new MockHttpServletRequest("GET", "/api/products/3"), new MockHttpServletResponse(), (a,b) -> {});
            assertEquals(0, metrics.get("catalog.read.active").gauge().value());
        } finally { release.countDown(); executor.shutdownNow(); }
    }
    @Test void sqlTimeoutIs503AndContextDoesNotLeak() throws Exception {
        var metrics = new SimpleMeterRegistry(); var filter = filter(metrics); var response = new MockHttpServletResponse();
        filter.doFilter(new MockHttpServletRequest("GET", "/api/products/1"), response, (a,b) -> {
            throw new ServletException(new SQLTimeoutException("secret SQL must not appear"));
        });
        assertEquals(503,response.getStatus()); assertFalse(response.getContentAsString().contains("secret"));
        assertEquals(1,metrics.counter("catalog.read.sql.timeout").count());
        var statement = mock(Statement.class); CatalogReadBudget.apply(statement); verifyNoInteractions(statement);
        assertEquals(0,metrics.get("catalog.read.active").gauge().value());
    }
    @Test void unrelatedFailurePropagatesAndReleasesPermit() throws Exception {
        var metrics=new SimpleMeterRegistry(); var filter=filter(metrics);
        assertThrows(IllegalStateException.class, () -> filter.doFilter(new MockHttpServletRequest("GET","/api/products/1"),
                new MockHttpServletResponse(), (a,b) -> { throw new IllegalStateException("bug"); }));
        assertEquals(0,metrics.get("catalog.read.active").gauge().value());
        assertEquals(0,metrics.counter("catalog.read.sql.timeout").count());
    }
    @Test void timeoutNeverExtendsExistingTransactionLimitAndRestoresNestedContext() throws Exception {
        var statement=mock(Statement.class); when(statement.getQueryTimeout()).thenReturn(1);
        try(var outer=CatalogReadBudget.open(2)) {
            CatalogReadBudget.apply(statement); verify(statement).setQueryTimeout(1);
            when(statement.getQueryTimeout()).thenReturn(0);
            try(var inner=CatalogReadBudget.open(3)) { CatalogReadBudget.apply(statement); verify(statement).setQueryTimeout(3); }
            CatalogReadBudget.apply(statement); verify(statement).setQueryTimeout(2);
        }
        clearInvocations(statement); CatalogReadBudget.apply(statement); verifyNoInteractions(statement);
    }
    @Test void scopeIncludesResolveButExcludesWritesAndOtherResources() {
        var filter=filter(new SimpleMeterRegistry());
        assertFalse(filter.shouldNotFilter(new MockHttpServletRequest("POST","/api/products/resolve")));
        assertFalse(filter.shouldNotFilter(new MockHttpServletRequest("GET","/api/products")));
        assertTrue(filter.shouldNotFilter(new MockHttpServletRequest("POST","/api/products")));
        assertTrue(filter.shouldNotFilter(new MockHttpServletRequest("GET","/api/orders")));
        assertTrue(filter.shouldNotFilter(new MockHttpServletRequest("GET","/api/products-other")));
    }
    @Test void invalidCapacityFailsStartupRatherThanSilentlyReservingNothing() {
        assertThrows(IllegalArgumentException.class, () -> new CatalogReadAdmissionFilter(new ObjectMapper(), Optional.empty(), 8,2,1000,8));
        assertThrows(IllegalArgumentException.class, () -> new CatalogReadAdmissionFilter(new ObjectMapper(), Optional.empty(), 1,0,1000,8));
        assertThrows(IllegalArgumentException.class, () -> new CatalogReadAdmissionFilter(new ObjectMapper(), Optional.empty(), 1,2,1001,8));
    }
    @Test void briefWaitAbsorbsTransientLoadWithoutRejecting() throws Exception {
        var metrics=new SimpleMeterRegistry();
        var filter=new CatalogReadAdmissionFilter(new ObjectMapper().findAndRegisterModules(),Optional.of(metrics),1,2,1000,3);
        var entered=new CountDownLatch(1); var release=new CountDownLatch(1);
        var executor=Executors.newFixedThreadPool(2);
        try {
            var first=executor.submit(() -> {
                filter.doFilter(new MockHttpServletRequest("GET","/api/products/1"),new MockHttpServletResponse(),(a,b)->{
                    entered.countDown();
                    try { assertTrue(release.await(3,TimeUnit.SECONDS)); }
                    catch(InterruptedException e){Thread.currentThread().interrupt();throw new ServletException(e);}
                });return null;
            });
            assertTrue(entered.await(2,TimeUnit.SECONDS));
            var response=new MockHttpServletResponse();
            var second=executor.submit(() -> {
                filter.doFilter(new MockHttpServletRequest("GET","/api/products/2"),response,(a,b)->{});return null;
            });
            Thread.sleep(50);
            assertFalse(second.isDone(), "a transiently busy read should wait rather than immediately reject");
            release.countDown();first.get(2,TimeUnit.SECONDS);second.get(2,TimeUnit.SECONDS);
            assertEquals(200,response.getStatus());assertEquals(0,metrics.counter("catalog.read.rejected").count());
        } finally {release.countDown();executor.shutdownNow();}
    }
    @Test void interruptedAdmissionDoesNotLeakPermit() throws Exception {
        var metrics=new SimpleMeterRegistry();var filter=filter(metrics);var response=new MockHttpServletResponse();
        try {
            Thread.currentThread().interrupt();
            filter.doFilter(new MockHttpServletRequest("GET","/api/products/1"),response,(a,b)->fail("interrupted"));
            assertEquals(503,response.getStatus());assertTrue(Thread.currentThread().isInterrupted());
            assertEquals(0,metrics.get("catalog.read.active").gauge().value());
        } finally {Thread.interrupted();}
    }
    @Test void zeroLimitDisablesAdmissionButRetainsSqlTimeout() throws Exception {
        var metrics=new SimpleMeterRegistry();
        var filter=new CatalogReadAdmissionFilter(new ObjectMapper().findAndRegisterModules(),Optional.of(metrics),0,2,1000,8);
        var response=new MockHttpServletResponse();
        filter.doFilter(new MockHttpServletRequest("GET","/api/products/1"),response,(a,b)->{
            throw new ServletException(new SQLTimeoutException("timeout"));
        });
        assertEquals(503,response.getStatus());
        assertEquals(0,metrics.counter("catalog.read.rejected").count());
        assertEquals(1,metrics.counter("catalog.read.sql.timeout").count());
    }
}
