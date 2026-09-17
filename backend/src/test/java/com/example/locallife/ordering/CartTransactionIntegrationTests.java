package com.example.locallife.ordering;

import com.example.locallife.common.*;
import com.example.locallife.fulfillment.*;
import com.example.locallife.integration.OutboxService;
import com.example.locallife.inventory.InventoryService;
import com.example.locallife.payment.*;
import org.junit.jupiter.api.*;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.test.web.servlet.MockMvc;
import com.fasterxml.jackson.databind.ObjectMapper;
import static org.springframework.security.test.web.servlet.request.SecurityMockMvcRequestPostProcessors.jwt;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.bean.override.mockito.*;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import java.time.Instant;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicLong;
import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

@SpringBootTest(properties={"local-life.fulfillment.enabled=true",
        "spring.datasource.url=jdbc:h2:mem:cart_transactions_v4;MODE=MySQL;DATABASE_TO_LOWER=TRUE;CASE_INSENSITIVE_IDENTIFIERS=TRUE;DB_CLOSE_DELAY=-1"})
@AutoConfigureMockMvc
class CartTransactionIntegrationTests {
    @Autowired MockMvc http;
    @Autowired ObjectMapper json;
    @Autowired CartOrderService carts;
    @Autowired OrderService orders;
    @Autowired InventoryService inventory;
    @Autowired PaymentService payments;
    @Autowired PaymentSignature signatures;
    @Autowired PartialRefundService refunds;
    @Autowired LocalPartialRefundSimulator provider;
    @Autowired FulfillmentEvents events;
    @Autowired FulfillmentClaims claims;
    @Autowired JdbcTemplate jdbc;
    @MockitoBean CommerceCatalogPort catalog;
    @MockitoSpyBean OutboxService outbox;
    static AtomicLong ids=new AtomicLong(700000);
    String user; long a,b;

    @BeforeEach void prepare() {
        assertThat(TransactionSynchronizationManager.isActualTransactionActive()).isFalse();
        user=UUID.randomUUID().toString();a=ids.incrementAndGet();b=ids.incrementAndGet();
        jdbc.update("INSERT INTO user_account(id,username,password_hash,enabled,token_version) VALUES(?,?,'x',TRUE,0)",user,user);
        // Intentionally invert catalog identifier and physical stock identifier order.
        inventory.createStock("PRODUCT",b,100);inventory.createStock("PRODUCT",a,100);
        when(catalog.requireItem(eq("PRODUCT"),anyLong())).thenAnswer(call->{
            long id=call.getArgument(1);return new CommerceItemSnapshot("PRODUCT",id,"test-"+id,id==a?101:202,"CNY",1,"{}");
        });
    }
    @Test void cartAllocationReplayRejectsDuplicateSkusAndKeepsOldApiCompatible() {
        var request=request(3,2,coupon(5));
        var first=carts.create(request,user,"cart-key");
        var reversed=new CreateCartOrderRequest(List.of(request.items().get(1),request.items().get(0)),request.userCouponId());
        assertThat(carts.create(reversed,user,"cart-key").id()).isEqualTo(first.id());
        assertThat(first.totalMinor()).isEqualTo(707);assertThat(first.payableMinor()).isEqualTo(702);
        assertThat(refunds.balance(first.id(),user)).extracting(PartialRefundService.Balance::discountMinor).containsExactly(2L,3L);
        assertThatThrownBy(()->carts.create(request(2,2,null),user,"cart-key")).isInstanceOf(BusinessConflictException.class);
        assertThatThrownBy(()->carts.create(new CreateCartOrderRequest(List.of(request.items().get(0),request.items().get(0)),null),user,"dup"))
                .isInstanceOf(InvalidBusinessStateException.class).hasMessageContaining("同一商品");
        var old=orders.create(new CreateOrderRequest("PRODUCT",a,1,null),user,"old-single");
        assertThat(old.items()).hasSize(1);
        orders.cancel(old.id(),user,false);orders.cancel(first.id(),user,false);
        assertStock(a,100,0,0);assertStock(b,100,0,0);
        record("allocation_and_old_api",first.id());
    }
    @Test void browserQuoteRejectsChangedPriceAndBindsTheIdempotencyKey() {
        var bad=new CreateCartOrderRequest(List.of(new CreateCartOrderRequest.Line("PRODUCT",a,2,100L)),null,200L);
        assertThatThrownBy(()->carts.create(bad,user,"stale-price")).isInstanceOf(BusinessConflictException.class);
        assertStock(a,100,0,0);
        var valid=new CreateCartOrderRequest(List.of(new CreateCartOrderRequest.Line("PRODUCT",a,2,101L)),null,202L);
        var order=carts.create(valid,user,"quoted-order");
        assertThat(carts.create(valid,user,"quoted-order").id()).isEqualTo(order.id());
        var changed=new CreateCartOrderRequest(valid.items(),null,201L);
        assertThatThrownBy(()->carts.create(changed,user,"quoted-order")).isInstanceOf(BusinessConflictException.class);
        orders.cancel(order.id(),user,false);
        assertStock(a,100,0,0);
    }
    @Test void refundQuoteIsReadOnlyAndChangedAmountRollsBackTheHold() {
        var order=paid(2,1,null);
        var request=new PartialRefundRequest(List.of(new PartialRefundRequest.Line(a,1)),"quote",101L);
        var quote=refunds.quote(order.id(),user,request);
        assertThat(quote.amountMinor()).isEqualTo(101);
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM partial_refund WHERE order_id=?",Integer.class,order.id())).isZero();
        assertThatThrownBy(()->refunds.create(order.id(),user,"stale-refund",
            new PartialRefundRequest(request.items(),request.reason(),100L))).isInstanceOf(BusinessConflictException.class);
        assertThat(taskStatus(order.id())).isNotEqualTo("REFUND_HOLD");
        var result=refunds.create(order.id(),user,"bound-refund",request);
        assertThat(refunds.byKey(order.id(),user,"bound-refund").id()).isEqualTo(result.id());
        assertThatThrownBy(()->refunds.byKey(order.id(),"stranger","bound-refund")).isInstanceOf(ForbiddenOperationException.class);
    }
    @Test void insufficientLaterStockRollsBackEarlierReservationCouponOrderAndOutbox() {
        jdbc.update("UPDATE inventory_stock SET available_quantity=0,total_quantity=0 WHERE item_id=?",a);
        String coupon=coupon(5);
        assertThatThrownBy(()->carts.create(request(1,1,coupon),user,"insufficient")).isInstanceOf(BusinessConflictException.class);
        assertStock(b,100,0,0);
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM customer_order WHERE user_id=?",Integer.class,user)).isZero();
        assertThat(jdbc.queryForObject("SELECT status FROM user_coupon WHERE id=?",String.class,coupon)).isEqualTo("AVAILABLE");
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM inventory_reservation WHERE stock_id IN (SELECT id FROM inventory_stock WHERE item_id IN (?,?))",Integer.class,a,b)).isZero();
    }
    @Test void cartOutboxFailureRollsBackBothReservedStocksAndEveryFinancialRow() {
        String coupon=coupon(5);
        doAnswer(call->{
            String id=call.getArgument(1);
            assertStock(a,97,3,0);assertStock(b,98,2,0);
            assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM order_line_allocation WHERE order_id=?",Integer.class,id)).isEqualTo(2);
            throw new IllegalStateException("cart-after-reserve-outbox-fault");
        }).when(outbox).append(eq("ORDER"),anyString(),eq("order.created.v1"),any());
        assertThatThrownBy(()->carts.create(request(3,2,coupon),user,"after-reserve-fault"))
                .hasMessageContaining("cart-after-reserve-outbox-fault");
        assertStock(a,100,0,0);assertStock(b,100,0,0);
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM customer_order WHERE user_id=?",Integer.class,user)).isZero();
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM order_line_allocation WHERE stock_id IN (SELECT id FROM inventory_stock WHERE item_id IN (?,?))",Integer.class,a,b)).isZero();
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM inventory_reservation WHERE stock_id IN (SELECT id FROM inventory_stock WHERE item_id IN (?,?))",Integer.class,a,b)).isZero();
        assertThat(jdbc.queryForObject("SELECT status FROM user_coupon WHERE id=?",String.class,coupon)).isEqualTo("AVAILABLE");
        System.out.println("V4_EVIDENCE creation_outbox_failure reserved_stocks=2 order_rows_after_rollback=0 allocation_rows_after_rollback=0");
    }
    @Test void inverseItemListsInConcurrentOrdersUseOneStockLockOrder() throws Exception {
        ExecutorService pool=Executors.newFixedThreadPool(4);
        try {
            CountDownLatch start=new CountDownLatch(1);var futures=new ArrayList<Future<?>>();
            for(int i=0;i<12;i++) { final int n=i; futures.add(pool.submit(()-> {
                await(start);var r=request(1,1,null);if(n%2==1) r=new CreateCartOrderRequest(List.of(r.items().get(1),r.items().get(0)),null);
                return carts.create(r,user,"parallel-"+n);
            })); }
            start.countDown();for(var f:futures) f.get(20,TimeUnit.SECONDS);
            assertStock(a,88,12,0);assertStock(b,88,12,0);
            System.out.println("V4_EVIDENCE reverse_cart_concurrency orders=12 success=12 reserved_per_sku=12");
        } finally { pool.shutdownNow(); }
    }
    @Test void partialRefundReceiptRecoveryRetainsImmutableVersionsAndExactMoney() {
        var order=paid(3,2,coupon(5));
        String original=jdbc.queryForObject("SELECT command_json FROM fulfillment_task WHERE order_id=?",String.class,order.id());
        var r=refunds.create(order.id(),user,"refund-1",refund(a,1));
        assertThat(r.amountMinor()).isEqualTo(101);
        assertThat(refunds.create(order.id(),user,"refund-1",refund(a,1)).id()).isEqualTo(r.id());
        assertThatThrownBy(()->refunds.create(order.id(),user,"refund-1",refund(a,2))).isInstanceOf(BusinessConflictException.class);
        events.reconcile(order.id());assertThat(taskStatus(order.id())).isEqualTo("REFUND_HOLD");
        assertThat(claims.claim(order.id(),"worker")).isEmpty();
        assertThat(refunds.reconcile(r.id(),user).status()).isEqualTo("PROCESSING");
        provider.succeed(r.id(),user);provider.succeed(r.id(),user); // Provider committed; application has not applied the receipt.
        assertStock(a,97,0,3);
        assertThat(refunds.get(r.id(),user).status()).isEqualTo("PROCESSING");
        assertThat(refunds.reconcile(r.id(),user).status()).isEqualTo("SUCCESS");
        refunds.reconcile(r.id(),user);assertStock(a,98,0,2);
        assertThat(jdbc.queryForObject("SELECT command_json FROM fulfillment_command_version WHERE order_id=? AND revision=1",String.class,order.id())).isEqualTo(original);
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM fulfillment_command_version WHERE order_id=?",Integer.class,order.id())).isEqualTo(2);
        var r2=refunds.create(order.id(),user,"refund-2",refund(a,1));assertThat(r2.amountMinor()).isEqualTo(100);
        provider.succeed(r2.id(),user);refunds.reconcile(r2.id(),user);
        var all=new PartialRefundRequest(List.of(new PartialRefundRequest.Line(a,1),new PartialRefundRequest.Line(b,2)),"remaining");
        var r3=refunds.create(order.id(),user,"refund-rest",all);assertThat(r3.amountMinor()).isEqualTo(501);
        provider.succeed(r3.id(),user);refunds.reconcile(r3.id(),user);
        assertThat(orders.get(order.id(),user,false).status()).isEqualTo("REFUNDED");assertThat(taskStatus(order.id())).isEqualTo("CANCELLED");
        assertStock(a,100,0,0);assertStock(b,100,0,0);
        assertThat(refunds.balance(order.id(),user).stream().mapToLong(PartialRefundService.Balance::refundedMinor).sum()).isEqualTo(702);
        assertThatThrownBy(()->refunds.create(order.id(),user,"over",refund(a,1))).isInstanceOf(BusinessConflictException.class);
        record("receipt_recovery_exact_101_100_501",order.id());
    }
    @Test void settlementOutboxFailureRollsBackLedgerStockAndCommandButKeepsProviderReceipt() {
        var order=paid(2,1,null);var r=refunds.create(order.id(),user,"refund",refund(a,1));provider.succeed(r.id(),user);
        doThrow(new IllegalStateException("v4-outbox-fault")).when(outbox).append(eq("ORDER"),eq(order.id()),eq("order.partial-refunded.v2"),any());
        assertThatThrownBy(()->refunds.reconcile(r.id(),user)).hasMessageContaining("v4-outbox-fault");
        assertThat(refunds.get(r.id(),user).status()).isEqualTo("PROCESSING");assertStock(a,98,0,2);
        assertThat(taskStatus(order.id())).isEqualTo("REFUND_HOLD");
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM fulfillment_command_version WHERE order_id=?",Integer.class,order.id())).isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM local_refund_receipt WHERE refund_id=?",Integer.class,r.id())).isEqualTo(1);
        doCallRealMethod().when(outbox).append(eq("ORDER"),eq(order.id()),eq("order.partial-refunded.v2"),any());
        refunds.reconcile(r.id(),user);assertStock(a,99,0,1);record("settlement_atomic_rollback",order.id());
    }
    @Test void concurrentRefundsCannotOverRefundAndDuplicateKeyHasOneLedger() throws Exception {
        var order=paid(1,1,null);ExecutorService pool=Executors.newFixedThreadPool(2);
        try {
            CountDownLatch start=new CountDownLatch(1);
            var f1=pool.submit(()->{await(start);return refunds.create(order.id(),user,"same",refund(a,1));});
            var f2=pool.submit(()->{await(start);return refunds.create(order.id(),user,"same",refund(a,1));});
            start.countDown();var r=f1.get(10,TimeUnit.SECONDS);assertThat(f2.get(10,TimeUnit.SECONDS).id()).isEqualTo(r.id());
            assertThatThrownBy(()->refunds.create(order.id(),user,"other",refund(a,1))).isInstanceOf(BusinessConflictException.class);
            provider.succeed(r.id(),user);refunds.reconcile(r.id(),user);
            assertThatThrownBy(()->refunds.create(order.id(),user,"over",refund(a,1))).isInstanceOf(BusinessConflictException.class);
            assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM partial_refund WHERE order_id=?",Integer.class,order.id())).isEqualTo(1);
            assertStock(a,100,0,0);assertStock(b,99,0,1);
            record("concurrent_refund_cap",order.id());
        } finally { pool.shutdownNow(); }
    }
    @Test void dispatchedUnknownLegacyFullAndWrongOwnerRefundsAreRejected() {
        var order=paid(1,1,null);events.reconcile(order.id());assertThat(claims.claim(order.id(),"owner")).isPresent();
        assertThatThrownBy(()->refunds.create(order.id(),user,"unsafe",refund(a,1))).isInstanceOf(BusinessConflictException.class);
        jdbc.update("UPDATE fulfillment_task SET status='UNKNOWN' WHERE order_id=?",order.id());
        assertThatThrownBy(()->refunds.create(order.id(),user,"unknown",refund(a,1))).isInstanceOf(BusinessConflictException.class);
        assertThatThrownBy(()->payments.requestRefund(order.id(),user,"legacy")).isInstanceOf(BusinessConflictException.class);
        assertThatThrownBy(()->refunds.create(order.id(),"other","owner",refund(a,1))).isInstanceOf(ForbiddenOperationException.class);
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM partial_refund WHERE order_id=?",Integer.class,order.id())).isZero();
    }
    @Test void mismatchedReceiptFailsClosedUntilEvidenceMatches() {
        var order=paid(1,1,null);var r=refunds.create(order.id(),user,"refund",refund(a,1));provider.succeed(r.id(),user);
        jdbc.update("UPDATE local_refund_receipt SET amount_minor=amount_minor+1 WHERE refund_id=?",r.id());
        assertThatThrownBy(()->refunds.reconcile(r.id(),user)).isInstanceOf(BusinessConflictException.class).hasMessageContaining("不匹配");
        assertStock(a,99,0,1);assertThat(taskStatus(order.id())).isEqualTo("REFUND_HOLD");
        assertThat(refunds.get(r.id(),user).status()).isEqualTo("PROCESSING");
    }
    @Test void paymentCallbackVersusExpiryRaceKeepsPaymentAndAllStocksAtomic() throws Exception {
        ExecutorService pool=Executors.newFixedThreadPool(2);int paid=0,expired=0;
        try {
            for(int i=0;i<12;i++) {
                var order=carts.create(request(1,1,null),user,"race-"+i);var payment=payments.create(order.id(),user);
                jdbc.update("UPDATE customer_order SET expires_at=DATEADD('MINUTE',-1,CURRENT_TIMESTAMP) WHERE id=?",order.id());
                var cb=new PaymentCallbackRequest(UUID.randomUUID().toString(),payment.paymentNo(),"race-"+UUID.randomUUID(),payment.amountMinor(),"SUCCESS",Instant.now().getEpochSecond());
                CountDownLatch start=new CountDownLatch(1);
                var p=pool.submit(()->{await(start);try {payments.processCallback(payment.provider(),cb,signatures.sign(cb));} catch(InvalidBusinessStateException expected) { }});
                var e=pool.submit(()->{await(start);orders.expireBatch(500);});start.countDown();p.get(10,TimeUnit.SECONDS);e.get(10,TimeUnit.SECONDS);
                String status=orders.get(order.id(),user,false).status();assertThat(status).isIn("PAID","EXPIRED");
                if(status.equals("PAID")) paid++;else expired++;
                assertThat(payments.getByOrderId(order.id(),user).status()).isEqualTo(status.equals("PAID")?"SUCCESS":"CREATED");
                assertThat(jdbc.queryForList("SELECT status FROM inventory_reservation WHERE order_id=?",String.class,order.id()))
                        .hasSize(2).containsOnly(status.equals("PAID")?"CONFIRMED":"EXPIRED");
            }
            assertStock(a,100-paid,0,paid);assertStock(b,100-paid,0,paid);
            System.out.println("V4_EVIDENCE payment_expiry_race total=12 paid="+paid+" expired="+expired+" inconsistent=0");
        } finally { pool.shutdownNow(); }
    }
    @Test void callbackWinningBeforeExpiryPreventsAllStockRelease() {
        var order=paid(2,3,null);
        jdbc.update("UPDATE customer_order SET expires_at=DATEADD('MINUTE',-1,CURRENT_TIMESTAMP) WHERE id=?",order.id());
        orders.expireBatch(500);
        assertThat(orders.get(order.id(),user,false).status()).isEqualTo("PAID");
        assertStock(a,98,0,2);assertStock(b,97,0,3);record("payment_first_expiry_second",order.id());
    }
    @Test void twelveConcurrentReconciliationsReturnOneSuccessfulRefundWithoutRepeatedRestock() throws Exception {
        var order=paid(2,1,null);var refund=refunds.create(order.id(),user,"repeat-reconcile",refund(a,1));provider.succeed(refund.id(),user);
        ExecutorService pool=Executors.newFixedThreadPool(12);
        try {
            CountDownLatch start=new CountDownLatch(1);var results=new ArrayList<Future<PartialRefundView>>();
            for(int i=0;i<12;i++) results.add(pool.submit(()->{await(start);return refunds.reconcile(refund.id(),user);}));
            start.countDown();for(var result:results) assertThat(result.get(15,TimeUnit.SECONDS).status()).isEqualTo("SUCCESS");
            assertStock(a,99,0,1);
            assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM fulfillment_command_version WHERE order_id=?",Integer.class,order.id())).isEqualTo(2);
            assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM partial_refund WHERE order_id=?",Integer.class,order.id())).isEqualTo(1);
            System.out.println("V4_EVIDENCE concurrent_reconcile requests=12 success=12 restocked_quantity=1 command_versions=2");
        } finally {pool.shutdownNow();}
    }
    @Test void distinctConcurrentRefundKeysHaveOneWinnerAndCannotExceedLinePaid() throws Exception {
        var order=paid(1,1,null);ExecutorService pool=Executors.newFixedThreadPool(2);
        try {
            CountDownLatch start=new CountDownLatch(1);
            var results=new ArrayList<Future<PartialRefundView>>();
            for(String key:List.of("first","second")) results.add(pool.submit(()-> {
                await(start);try{return refunds.create(order.id(),user,key,refund(a,1));}
                catch(BusinessConflictException expected){return null;}
            }));
            start.countDown();var winners=new ArrayList<PartialRefundView>();
            for(var f:results){var value=f.get(10,TimeUnit.SECONDS);if(value!=null) winners.add(value);}
            assertThat(winners).hasSize(1);var r=winners.get(0);provider.succeed(r.id(),user);refunds.reconcile(r.id(),user);
            assertThat(jdbc.queryForObject("SELECT SUM(amount_minor) FROM partial_refund WHERE order_id=?",Long.class,order.id())).isEqualTo(101);
            assertStock(a,100,0,0);record("distinct_refund_race_one_winner",order.id());
        } finally {pool.shutdownNow();}
    }
    @Test void dispatchAndPartialRefundShareOrderLockAndHaveExactlyOneWinner() throws Exception {
        var order=paid(1,1,null);events.reconcile(order.id());ExecutorService pool=Executors.newFixedThreadPool(2);
        try {
            CountDownLatch start=new CountDownLatch(1);
            var dispatch=pool.submit(()->{await(start);return claims.claim(order.id(),"racing-worker").isPresent();});
            var refund=pool.submit(()->{await(start);try{refunds.create(order.id(),user,"racing-refund",refund(a,1));return true;}
                catch(BusinessConflictException expected){return false;}});
            start.countDown();assertThat(dispatch.get(10,TimeUnit.SECONDS)^refund.get(10,TimeUnit.SECONDS)).isTrue();
            assertThat(taskStatus(order.id())).isIn("DISPATCHING","REFUND_HOLD");record("dispatch_refund_race",order.id());
        } finally {pool.shutdownNow();}
    }
    @Test void cartAndPartialRefundHttpContractsValidateOwnershipAndKeepEnvelope() throws Exception {
        String body=json.writeValueAsString(request(2,1,null));
        http.perform(post("/api/orders/cart").contentType("application/json").content(body)).andExpect(status().isUnauthorized());
        String created=http.perform(post("/api/orders/cart").with(jwt().jwt(j->j.subject(user)))
                .header("Idempotency-Key","http-cart").contentType("application/json").content(body))
                .andExpect(status().isCreated()).andExpect(jsonPath("$.success").value(true)).andExpect(jsonPath("$.data.items.length()").value(2))
                .andReturn().getResponse().getContentAsString();
        String id=json.readTree(created).path("data").path("id").asText();var payment=payments.create(id,user);payments.simulateSuccess(payment.id(),user);
        http.perform(post("/api/payments/orders/{id}/refunds",id).with(jwt().jwt(j->j.subject(user)))
                .contentType("application/json").content("{\"reason\":\"legacy\"}")).andExpect(status().isConflict());
        String rbody=json.writeValueAsString(refund(a,1));
        http.perform(post("/api/payments/orders/{id}/partial-refunds",id).with(jwt().jwt(j->j.subject("stranger")))
                .header("Idempotency-Key","stranger").contentType("application/json").content(rbody)).andExpect(status().isForbidden());
        String rcreated=http.perform(post("/api/payments/orders/{id}/partial-refunds",id).with(jwt().jwt(j->j.subject(user)))
                .header("Idempotency-Key","http-refund").contentType("application/json").content(rbody))
                .andExpect(status().isCreated()).andExpect(jsonPath("$.data.status").value("PROCESSING"))
                .andExpect(jsonPath("$.data.amountMinor").value(101)).andReturn().getResponse().getContentAsString();
        String refundId=json.readTree(rcreated).path("data").path("id").asText();
        http.perform(post("/api/payments/partial-refunds/{id}/simulate-success",refundId).with(jwt().jwt(j->j.subject(user))))
                .andExpect(status().isOk()).andExpect(jsonPath("$.data.status").value("SUCCESS"));
        http.perform(post("/api/payments/partial-refunds/{id}/reconcile",refundId).with(jwt().jwt(j->j.subject(user))))
                .andExpect(status().isOk()).andExpect(jsonPath("$.data.status").value("SUCCESS"));
        assertStock(a,99,0,1);
    }
    private CreateCartOrderRequest request(int qa,int qb,String coupon) {return new CreateCartOrderRequest(List.of(new CreateCartOrderRequest.Line("PRODUCT",a,qa),new CreateCartOrderRequest.Line("PRODUCT",b,qb)),coupon);}
    private PartialRefundRequest refund(long id,int quantity) {return new PartialRefundRequest(List.of(new PartialRefundRequest.Line(id,quantity)),"unshipped");}
    private OrderResponse paid(int qa,int qb,String coupon) {var order=carts.create(request(qa,qb,coupon),user,UUID.randomUUID().toString());var payment=payments.create(order.id(),user);payments.simulateSuccess(payment.id(),user);return order;}
    private String coupon(int discount) {
        long template=ids.incrementAndGet();String id=UUID.randomUUID().toString();
        jdbc.update("INSERT INTO coupon_template(id,name,threshold_minor,discount_minor,total_quantity,claimed_quantity,valid_from,valid_until,status,version) VALUES(?,'test',0,?,100,1,DATEADD('DAY',-1,CURRENT_TIMESTAMP),DATEADD('DAY',1,CURRENT_TIMESTAMP),'ACTIVE',0)",template,discount);
        jdbc.update("INSERT INTO user_coupon(id,template_id,user_id,status,version) VALUES(?,?,?,'AVAILABLE',0)",id,template,user);return id;
    }
    private void assertStock(long id,int available,int reserved,int sold) {var s=inventory.getStock("PRODUCT",id);assertThat(s.availableQuantity()).isEqualTo(available);assertThat(s.reservedQuantity()).isEqualTo(reserved);assertThat(s.soldQuantity()).isEqualTo(sold);}
    private String taskStatus(String id) {return jdbc.queryForObject("SELECT status FROM fulfillment_task WHERE order_id=?",String.class,id);}
    private static void await(CountDownLatch latch) {try {if(!latch.await(5,TimeUnit.SECONDS)) throw new IllegalStateException("barrier timeout");}catch(InterruptedException e){Thread.currentThread().interrupt();throw new IllegalStateException(e);}}
    private void record(String scenario,String id) {System.out.println("V4_EVIDENCE scenario="+scenario+" order="+id+" status="+orders.get(id,user,false).status()+" fulfillment="+taskStatus(id)+" balance="+refunds.balance(id,user));}
}
