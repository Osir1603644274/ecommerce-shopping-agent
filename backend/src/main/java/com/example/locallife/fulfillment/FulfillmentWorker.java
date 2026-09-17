package com.example.locallife.fulfillment;

import io.micrometer.core.instrument.MeterRegistry;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import jakarta.annotation.PreDestroy;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.*;

@Component
@ConditionalOnProperty(prefix = "local-life.fulfillment", name = "worker-enabled", havingValue = "true")
public class FulfillmentWorker {
    private final FulfillmentClaims claims;
    private final FulfillmentEvents events;
    private final FulfillmentStore store;
    private final WarehouseGateway warehouse;
    private final MeterRegistry meters;
    private final ThreadPoolExecutor pool;
    private final Set<String> queued = ConcurrentHashMap.newKeySet();
    private final String instance = UUID.randomUUID().toString();

    FulfillmentWorker(FulfillmentClaims claims, FulfillmentEvents events, FulfillmentStore store,
            WarehouseGateway warehouse, FulfillmentProperties properties, MeterRegistry meters) {
        this.claims = claims; this.events = events; this.store = store; this.warehouse = warehouse; this.meters = meters;
        pool = new ThreadPoolExecutor(properties.workers(), properties.workers(), 0, TimeUnit.SECONDS,
                new ArrayBlockingQueue<>(properties.queueSize()), runnable -> {
                    Thread thread = new Thread(runnable, "fulfillment-worker"); thread.setDaemon(true); return thread;
                }, new ThreadPoolExecutor.AbortPolicy());
        meters.gauge("local.life.fulfillment.worker.active", pool, ThreadPoolExecutor::getActiveCount);
        meters.gauge("local.life.fulfillment.worker.queued", pool, executor -> executor.getQueue().size());
    }

    @Scheduled(fixedDelayString = "${local-life.fulfillment.poll-delay:PT1S}")
    public void poll() {
        for (String id : claims.candidates(100)) {
            if (!queued.add(id)) continue;
            try { pool.execute(() -> { try { runOne(id); } finally { queued.remove(id); } }); }
            catch (RejectedExecutionException full) {
                queued.remove(id); meters.counter("local.life.fulfillment.queue.rejected").increment(); break;
            }
        }
    }

    public void runOne(String id) {
        claims.claim(id, instance).ifPresent(task -> {
            var sample = io.micrometer.core.instrument.Timer.start(meters);
            String outcome = "unknown";
            try {
                WarehouseReceipt receipt = warehouse.lookup(task.requestKey()).orElseGet(() -> warehouse.dispatch(task));
                outcome = claims.shipped(task, receipt) ? "shipped" : "stale";
            } catch (RuntimeException error) {
                claims.failed(task, error.getMessage());
            } finally {
                sample.stop(meters.timer("local.life.fulfillment.dispatch", "outcome", outcome));
            }
        });
    }

    @Scheduled(fixedDelayString = "${local-life.fulfillment.reconcile-delay:PT30S}")
    public void reconcile() {
        // Durable enrollment makes paid-but-not-delivered events discoverable without inventing a Kafka receipt.
        var ids = store.jdbc().query("""
            SELECT f.order_id FROM fulfillment_task f JOIN customer_order o ON o.id=f.order_id
            WHERE f.status='WAITING_PAYMENT' AND o.status&lt;&gt;'PENDING_PAYMENT'
            ORDER BY f.created_at LIMIT 100
            """.replace("&lt;&gt;", "<>"), (rs, n) -> rs.getString(1));
        ids.forEach(events::reconcile);
    }

    @PreDestroy void close() { pool.shutdownNow(); }
}
