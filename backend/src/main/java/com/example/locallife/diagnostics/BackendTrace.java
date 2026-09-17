package com.example.locallife.diagnostics;

import java.util.*;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;

/** Observations only. Business arguments are allowlisted; no credentials or raw entities. */
public final class BackendTrace {
    private static final ThreadLocal<Context> LOCAL = new ThreadLocal<>();
    public static final class Event {
        public final String kind, name;
        public final String id=UUID.randomUUID().toString();
        public String parentId;
        public final String startedAt=java.time.Instant.now().toString();
        public String finishedAt;
        public Map<String,Object> input=Map.of(), source=Map.of();
        public Object output;
        private final long startedNanos = System.nanoTime();
        public double durationMs;
        public String outcome = "running";
        public Integer affectedRows;
        Event(String kind, String name) { this.kind=kind; this.name=name; }
    }
    public static final class Context {
        public final String id=UUID.randomUUID().toString();
        public final String method, path;
        public final List<Event> events=new ArrayList<>();
        private final Deque<Event> stack=new ArrayDeque<>();
        public int httpStatus, droppedEvents;
        Context(String method,String path) { this.method=method; this.path=path; }
    }
    public static Context begin(String method,String path) { var c=new Context(method,path); LOCAL.set(c); return c; }
    public static Context current() { return LOCAL.get(); }
    public static void clear() { LOCAL.remove(); }
    public static void mark(String kind,String name,String outcome) {
        var event=start(kind,name);
        if(event!=null) event.source=switch(kind) {
            case "cache" -> BackendTraceSource.lookup("com.example.locallife.product.ProductCache","getDetail");
            case "rate_limit" -> BackendTraceSource.lookup("com.example.locallife.identity.SlidingWindowRateLimiter","acquire");
            case "redis_lua" -> BackendTraceSource.lookup("com.example.locallife.flashsale.FlashSaleRedisGateway","purchase");
            default -> Map.of();
        };
        finish(event,outcome,null);
    }
    public static Event start(String kind,String name) {
        var c=current(); if(c==null) return null;
        if(c.events.size()>=150) { c.droppedEvents++; return null; }
        var event=new Event(kind,name); event.parentId=c.stack.isEmpty()?null:c.stack.peek().id;
        c.events.add(event);
        if(!kind.equals("transaction")) c.stack.push(event);
        return event;
    }
    public static void finish(Event event,String outcome,Integer rows) {
        if(event==null) return;
        event.durationMs=(System.nanoTime()-event.startedNanos)/1_000_000.0;
        event.finishedAt=java.time.Instant.now().toString();
        var c=current(); if(c!=null) c.stack.remove(event);
        event.outcome=outcome; event.affectedRows=rows;
    }
    public static void observeTransaction() {
        var c=current();
        if(c==null || !TransactionSynchronizationManager.isSynchronizationActive()
                || !TransactionSynchronizationManager.isActualTransactionActive()) return;
        if(TransactionSynchronizationManager.getSynchronizations().stream().anyMatch(s->s instanceof Completion)) return;
        var e=start("transaction",TransactionSynchronizationManager.isCurrentTransactionReadOnly()?"只读事务":"数据库事务");
        if(e!=null) e.input=Map.of("observationBoundary","从首条已接入 SQL 处开始观测，到 afterCompletion；不是事务精确起点");
        if(e!=null) e.source=BackendTraceSource.lookup("com.example.locallife.diagnostics.BackendTrace","observeTransaction");
        TransactionSynchronizationManager.registerSynchronization(new Completion(e));
    }
    private static final class Completion implements TransactionSynchronization {
        private final Event event;
        Completion(Event event) { this.event=event; }
        public void afterCompletion(int status) {
            finish(event,status==STATUS_COMMITTED?"committed":status==STATUS_ROLLED_BACK?"rolled_back":"unknown",null);
        }
    }
    private BackendTrace() {}
}
