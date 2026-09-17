package com.example.locallife.fulfillment;

import com.example.locallife.integration.EventEnvelope;
import com.example.locallife.inventory.InventoryService;
import com.example.locallife.ordering.*;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.bean.override.mockito.MockitoSpyBean;
import java.time.LocalDateTime;
import java.util.UUID;
import java.util.concurrent.*;
import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

@SpringBootTest(properties = {"local-life.fulfillment.enabled=true",
        "local-life.fulfillment.kafka-enabled=true", "spring.kafka.listener.auto-startup=false",
        "spring.datasource.url=jdbc:h2:mem:fulfillment_commit;MODE=MySQL;DATABASE_TO_LOWER=TRUE;CASE_INSENSITIVE_IDENTIFIERS=TRUE;DB_CLOSE_DELAY=-1"})
class FulfillmentCommitTests {
    @Autowired OrderService orders;
    @Autowired InventoryService inventory;
    @Autowired FulfillmentEvents events;
    @Autowired FulfillmentClaims claims;
    @Autowired JdbcTemplate jdbc;
    @Autowired FulfillmentProperties properties;
    @MockitoSpyBean FulfillmentStore store;
    String id;

    @BeforeEach void prepare() {
        String user = UUID.randomUUID().toString();
        jdbc.update("INSERT INTO user_account(id,username,password_hash,enabled,token_version) VALUES(?,?,'x',TRUE,0)", user, user);
        if (jdbc.queryForObject("SELECT COUNT(*) FROM inventory_stock WHERE item_type='PRODUCT' AND item_id=1001", Integer.class) == 0)
            inventory.createStock("PRODUCT", 1001L, 100);
        id = orders.create(new CreateOrderRequest("PRODUCT", 1001L, 1, null), user, UUID.randomUUID().toString()).id();
        orders.markPaid(id);
    }

    EventEnvelope event() { return new EventEnvelope(UUID.randomUUID().toString(), "ORDER", id, "order.paid.v1", "{}", LocalDateTime.now()); }

    @Test void remoteInventoryMustSettleBeforeWarehouseCanClaimPaidOrder() {
        events.accept(event());
        var settlement = mock(com.example.locallife.inventory.InventorySettlement.class);
        claims.setInventoryJournal(settlement);
        try {
            when(settlement.settled(id)).thenReturn(false);
            assertThat(claims.claim(id, "worker-before-confirm")).isEmpty();
            assertThat(store.find(id).status()).isEqualTo("READY");
            assertThat(store.find(id).attempts()).isZero();
            when(settlement.settled(id)).thenReturn(true);
            assertThat(claims.claim(id, "worker-after-confirm")).isPresent();
            assertThat(store.find(id).status()).isEqualTo("DISPATCHING");
        } finally {
            claims.setInventoryJournal(null);
        }
    }

    @Test void handlerFailureRollsBackTaskTransitionAndReceiptTogetherThenReplayCommits() {
        EventEnvelope event = event();
        doThrow(new IllegalStateException("injected database failure")).when(store).audit(eq(id), anyLong(), eq("READY"), anyString());
        assertThatThrownBy(() -> events.accept(event)).hasMessageContaining("injected");
        assertThat(store.find(id).status()).isEqualTo("WAITING_PAYMENT");
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM inbox_event WHERE consumer_name='fulfillment-v1' AND event_id=?", Integer.class, event.id())).isZero();
        doCallRealMethod().when(store).audit(eq(id), anyLong(), eq("READY"), anyString());
        events.accept(event);
        events.accept(event); // Process died after commit, before broker acknowledgment.
        assertThat(store.find(id).status()).isEqualTo("READY");
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM inbox_event WHERE consumer_name='fulfillment-v1' AND event_id=?", Integer.class, event.id())).isEqualTo(1);
    }

    @Test void concurrentDeliveryCommitsOnceAndDispatchRefundRaceHasOneWinner() throws Exception {
        EventEnvelope event = event();
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            CountDownLatch start = new CountDownLatch(1);
            Future<?> a = pool.submit(() -> { await(start); events.accept(event); });
            Future<?> b = pool.submit(() -> { await(start); events.accept(event); });
            start.countDown(); a.get(10, TimeUnit.SECONDS); b.get(10, TimeUnit.SECONDS);
            CountDownLatch race = new CountDownLatch(1);
            Future<Boolean> dispatch = pool.submit(() -> { await(race); return claims.claim(id, "worker").isPresent(); });
            Future<Boolean> refund = pool.submit(() -> {
                await(race);
                try { orders.markRefunding(id); return true; }
                catch (com.example.locallife.common.BusinessConflictException expected) { return false; }
            });
            race.countDown();
            assertThat(dispatch.get(10, TimeUnit.SECONDS) ^ refund.get(10, TimeUnit.SECONDS)).isTrue();
            assertThat(store.find(id).status()).isIn("DISPATCHING", "CANCELLED");
            assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM fulfillment_attempt WHERE order_id=? AND outcome='READY'", Integer.class, id)).isEqualTo(1);
        } finally { pool.shutdownNow(); }
    }

    @Test void workerReconcilesCommittedExternalEffectAfterLostResponseWithoutRedispatch() {
        events.accept(event());
        var receipts = new java.util.HashMap<String, WarehouseReceipt>();
        var dispatches = new java.util.concurrent.atomic.AtomicInteger();
        WarehouseGateway warehouse = new WarehouseGateway() {
            public java.util.Optional<WarehouseReceipt> lookup(String key) {
                return java.util.Optional.ofNullable(receipts.get(key));
            }
            public WarehouseReceipt dispatch(FulfillmentTask task) {
                dispatches.incrementAndGet();
                receipts.put(task.requestKey(), new WarehouseReceipt(task.requestKey(), task.orderId(),
                        FulfillmentEvents.hash(task.commandJson()), "TRACK-LOST-RESPONSE"));
                throw new IllegalStateException("Response lost after durable warehouse commit");
            }
        };
        var worker = new FulfillmentWorker(claims, events, store, warehouse, properties,
                new io.micrometer.core.instrument.simple.SimpleMeterRegistry());
        try {
            worker.runOne(id);
            assertThat(store.find(id).status()).isEqualTo("UNKNOWN");
            assertThat(receipts).hasSize(1);
            jdbc.update("UPDATE fulfillment_task SET next_attempt_at=DATEADD('SECOND',-1,CURRENT_TIMESTAMP) WHERE order_id=?", id);
            worker.runOne(id);
            worker.runOne(id);
            assertThat(store.find(id).status()).isEqualTo("SHIPPED");
            assertThat(store.find(id).trackingNo()).isEqualTo("TRACK-LOST-RESPONSE");
            assertThat(dispatches.get()).isEqualTo(1);
        } finally { worker.close(); }
    }

    private static void await(CountDownLatch latch) {
        try { if (!latch.await(5, TimeUnit.SECONDS)) throw new IllegalStateException("barrier timeout"); }
        catch (InterruptedException e) { Thread.currentThread().interrupt(); throw new IllegalStateException(e); }
    }
}
