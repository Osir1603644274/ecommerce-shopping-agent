package com.example.locallife.diagnostics;

import org.junit.jupiter.api.*;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;
import org.springframework.mock.web.*;
import org.springframework.web.server.ResponseStatusException;
import org.springframework.jdbc.datasource.*;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.support.TransactionTemplate;
import org.mybatis.spring.*;
import org.apache.ibatis.annotations.*;
import java.util.*;

class BackendObserverTest {
    static final String KEY="isolated-test-observer-key-32-chars-long";
    @AfterEach void clear() { BackendTrace.clear(); }
    MockHttpServletRequest request(boolean trusted) {
        var r=new MockHttpServletRequest("GET","/api/orders/12345");
        if(trusted) r.addHeader(BackendTraceFilter.KEY_HEADER,KEY);
        return r;
    }
    @Test void ordinaryRequestHasNoTrace() throws Exception {
        var response=new MockHttpServletResponse();
        new BackendTraceFilter(new BackendTraceStore(),KEY).doFilter(request(false),response,(r,s)->assertNull(BackendTrace.current()));
        assertNull(response.getHeader("X-Java-Trace-Id"));
    }
    @Test void featureIsOffUnlessExplicitlyEnabled() {
        new org.springframework.boot.test.context.runner.ApplicationContextRunner()
            .withUserConfiguration(BackendTraceStore.class,BackendTraceFilter.class,BackendTraceController.class,BackendTraceAspect.class,BackendSqlObserver.class)
            .run(context->{ assertTrue(context.getBeansOfType(BackendTraceFilter.class).isEmpty());
                assertTrue(context.getBeansOfType(BackendTraceAspect.class).isEmpty());
                assertTrue(context.getBeansOfType(BackendSqlObserver.class).isEmpty()); });
    }
    @Test void trustedRequestRecordsActualStatusAndCleansThread() throws Exception {
        var store=new BackendTraceStore(); var response=new MockHttpServletResponse();
        new BackendTraceFilter(store,KEY).doFilter(request(true),response,(r,s)->((MockHttpServletResponse)s).setStatus(403));
        var trace=store.get(response.getHeader("X-Java-Trace-Id"));
        assertEquals(403,trace.httpStatus); assertEquals("/api/orders/:id",trace.path);
        assertEquals(1,trace.events.size()); assertNull(BackendTrace.current());
    }
    @Test void diagnosticStoreFailureCannotFailBusiness() throws Exception {
        var store=mock(BackendTraceStore.class); doThrow(new IllegalStateException()).when(store).save(any());
        var response=new MockHttpServletResponse();
        assertDoesNotThrow(()->new BackendTraceFilter(store,KEY).doFilter(request(true),response,(r,s)->s.getWriter().write("ok")));
        assertEquals("ok",response.getContentAsString()); assertNull(BackendTrace.current());
    }
    @Test void businessFailureIsPreservedAndThreadCleared() {
        var store=new BackendTraceStore(); var response=new MockHttpServletResponse();
        var expected=new IllegalArgumentException("private payload");
        assertSame(expected,assertThrows(IllegalArgumentException.class,()->new BackendTraceFilter(store,KEY)
            .doFilter(request(true),response,(r,s)->{throw expected;})));
        assertEquals(500,store.get(response.getHeader("X-Java-Trace-Id")).httpStatus); assertNull(BackendTrace.current());
    }
    @Test void traceReadRequiresServerKeyAndShortKeysAreRejected() {
        var store=new BackendTraceStore(); var context=BackendTrace.begin("GET","/api/orders"); store.save(context);
        var controller=new BackendTraceController(store,new BackendTraceFilter(store,KEY));
        assertThrows(ResponseStatusException.class,()->controller.get(context.id,request(false)));
        assertNotNull(controller.get(context.id,request(true)));
        assertFalse(new BackendTraceFilter(store,"short").authorized(request(true)));
    }
    @Test void eventsAreBoundedAndDoNotCapturePayloads() throws Exception {
        var c=BackendTrace.begin("GET","/api/orders");
        for(int i=0;i<170;i++) BackendTrace.start("method","safeName");
        assertEquals(150,c.events.size()); assertEquals(20,c.droppedEvents);
        var json=new com.fasterxml.jackson.databind.ObjectMapper().writeValueAsString(c);
        assertFalse(json.contains("Authorization")); assertFalse(json.contains("parameters"));
    }
    interface Statements {
        @Select("select id from item order by id") List<Integer> all();
        @Select("select id from item where id=#{id}") List<Integer> one(int id);
        @Insert("insert into item(id) values(#{id})") int insert(int id);
    }
    record Fixture(Statements mapper,TransactionTemplate tx,JdbcTemplate jdbc) {}
    Fixture fixture() throws Exception {
        var ds=new DriverManagerDataSource("jdbc:h2:mem:"+UUID.randomUUID()+";DB_CLOSE_DELAY=-1","sa","");
        var jdbc=new JdbcTemplate(ds); jdbc.execute("create table item(id int primary key)"); jdbc.update("insert into item values(1),(2)");
        var config=new org.apache.ibatis.session.Configuration(); config.addMapper(Statements.class);
        var factory=new SqlSessionFactoryBean(); factory.setDataSource(ds); factory.setConfiguration(config); factory.setPlugins(new BackendSqlObserver());
        var mapper=new SqlSessionTemplate(factory.getObject()).getMapper(Statements.class);
        return new Fixture(mapper,new TransactionTemplate(new DataSourceTransactionManager(ds)),jdbc);
    }
    @Test void realH2QueriesCountPhysicalStatementsNotLocalCache() throws Exception {
        var f=fixture(); var c=BackendTrace.begin("GET","/api/orders");
        f.tx.executeWithoutResult(status->{ assertEquals(2,f.mapper.all().size()); f.mapper.all(); f.mapper.one(1); });
        var sql=c.events.stream().filter(e->e.kind.equals("sql")).toList();
        assertEquals(2,sql.size()); assertTrue(sql.get(0).name.contains("Statements.all"));
        assertEquals(2,sql.get(0).affectedRows);
        assertTrue(c.events.stream().anyMatch(e->e.kind.equals("transaction")&&e.outcome.equals("committed")));
    }
    @Test void actualRollbackIsObserved() throws Exception {
        var f=fixture(); var c=BackendTrace.begin("POST","/api/orders");
        f.tx.executeWithoutResult(status->{f.mapper.insert(3);status.setRollbackOnly();});
        assertTrue(c.events.stream().anyMatch(e->e.kind.equals("transaction")&&e.outcome.equals("rolled_back")));
        assertEquals(2,f.jdbc.queryForObject("select count(*) from item",Integer.class));
    }
    @Test void detailedEventsHaveRealTimesParentAndSafeSqlBindings() throws Exception {
        var f=fixture(); var c=BackendTrace.begin("GET","/api/orders");
        var parent=BackendTrace.start("method","test-parent");
        f.mapper.one(1); BackendTrace.finish(parent,"returned",null);
        var sql=c.events.stream().filter(e->e.kind.equals("sql")).findFirst().orElseThrow();
        assertEquals(parent.id,sql.parentId);
        assertNotNull(sql.startedAt); assertNotNull(sql.finishedAt);
        assertFalse(java.time.Instant.parse(sql.finishedAt).isBefore(java.time.Instant.parse(sql.startedAt)));
        assertTrue(sql.input.get("sqlTemplate").toString().contains("where id=?"));
        assertTrue(sql.input.get("bindings").toString().contains("1"));
    }
    @Test void onlyPackagedSourceAndAllowlistedFieldsAreReadable() {
        var source=BackendTraceSource.lookup("com.example.locallife.ordering.CartOrderService","create");
        assertTrue(source.get("file").toString().endsWith("CartOrderService.java"));
        assertTrue(source.containsKey("line")); assertEquals(64,source.get("sha256").toString().length());
        assertFalse(BackendTraceSource.lookup("com.example.locallife.identity.SecurityConfiguration","writeError").containsKey("snippet"));
        assertEquals("[未公开字段]",BackendTraceValues.value("password","private",0));
        assertEquals(2,BackendTraceValues.value("quantity",2,0));
    }
    @Test void productExplanationIncludesRealControllerAndSqlCode() {
        var source=BackendTraceSource.lookup("com.example.locallife.product.ProductMapper","findById");
        assertTrue(source.get("snippet").toString().contains("@Select"));
        assertTrue(source.get("snippet").toString().contains("FROM product WHERE id"));
        assertFalse(source.get("snippet").toString().contains("@Insert"));
        assertTrue(BackendTraceSource.lookup("com.example.locallife.product.ProductOfferController","get").get("snippet").toString().contains("offers.find(id)"));
        assertTrue(BackendTraceSource.lookup("com.example.locallife.product.LocalOfferService","find").get("snippet").toString().contains("FROM product_local_offer"));
        BackendTrace.begin("GET","/api/products/1");
        BackendTrace.mark("rate_limit","Redis ZSET + Lua","allowed");
        assertTrue(BackendTrace.current().events.get(0).source.containsKey("snippet"));
    }
    @Test void realSpringProxyCapturesMethodButNotArgumentsOrExceptionMessage() throws Exception {
        var factory=new org.springframework.aop.aspectj.annotation.AspectJProxyFactory(new com.example.locallife.ordering.ObserverProbeService());
        factory.addAspect(new BackendTraceAspect());
        com.example.locallife.ordering.ObserverProbeService proxy=factory.getProxy();
        var c=BackendTrace.begin("POST","/api/orders");
        assertEquals(7,proxy.count("secret-password"));
        assertThrows(IllegalStateException.class,proxy::fail);
        assertEquals(2,c.events.size());
        assertEquals("returned",c.events.get(0).outcome);
        assertEquals("threw:IllegalStateException",c.events.get(1).outcome);
        String json=new com.fasterxml.jackson.databind.ObjectMapper().writeValueAsString(c);
        assertFalse(json.contains("secret-password")); assertFalse(json.contains("private exception detail"));
    }
}
