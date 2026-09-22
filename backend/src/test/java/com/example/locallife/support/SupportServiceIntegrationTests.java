package com.example.locallife.support;

import com.example.locallife.common.*;
import com.example.locallife.ordering.*;
import com.example.locallife.inventory.InventoryService;
import com.example.locallife.payment.*;
import org.junit.jupiter.api.*;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicLong;
import static com.example.locallife.support.AfterSalePolicy.Type;
import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;
import static org.mockito.ArgumentMatchers.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;
import static org.springframework.security.test.web.servlet.request.SecurityMockMvcRequestPostProcessors.jwt;

@SpringBootTest(properties={"local-life.fulfillment.enabled=true","local-life.support.enabled=true","local-life.support.simulator-enabled=true",
        "spring.datasource.url=jdbc:h2:mem:support_v1;MODE=MySQL;DATABASE_TO_LOWER=TRUE;CASE_INSENSITIVE_IDENTIFIERS=TRUE;DB_CLOSE_DELAY=-1"})
@AutoConfigureMockMvc
class SupportServiceIntegrationTests {
    @Autowired SupportService support;
    @Autowired JdbcTemplate jdbc;
    @Autowired CartOrderService carts;
    @Autowired OrderService orders;
    @Autowired PaymentService payments;
    @Autowired PartialRefundService oldRefunds;
    @Autowired InventoryService inventory;
    @Autowired MockMvc http;
    @Autowired SaleSpecificationRegistry specifications;
    @Autowired SupportReceiptSimulator simulator;
    @Autowired SupportOperations operations;
    @Autowired SupportReceiptRecovery recovery;
    @Autowired SupportStockEffects stockEffects;
    @Autowired SupportTickets tickets;
    @Autowired SupportOrderSimulator orderSimulator;
    @Autowired SupportScenarioClock scenarioClock;
    @MockitoBean CommerceCatalogPort catalog;
    static AtomicLong ids=new AtomicLong(820000);
    String user;long item;
    @BeforeEach void setup() {
        user=UUID.randomUUID().toString();item=ids.incrementAndGet();
        jdbc.update("INSERT INTO user_account(id,username,password_hash,enabled,token_version) VALUES(?,?,'x',TRUE,0)",user,user);
        inventory.createStock("PRODUCT",item,20);
        when(catalog.requireItem(eq("PRODUCT"),eq(item))).thenReturn(new CommerceItemSnapshot("PRODUCT",item,"support fixture",101,"CNY",1,
                "{\"saleSpecification\":{\"code\":\"black-256\",\"color\":\"black\",\"storage\":\"256GB\"}}"));
    }
    private String paid(boolean received) {
        var order=carts.create(new CreateCartOrderRequest(List.of(new CreateCartOrderRequest.Line("PRODUCT",item,3)),null),user,UUID.randomUUID().toString());
        var payment=payments.create(order.id(),user);payments.simulateSuccess(payment.id(),user);
        if(received) {
            // Fixture transport event, then the actual owner-confirmation transaction.
            jdbc.update("UPDATE fulfillment_task SET status='SHIPPED',fence=1 WHERE order_id=?",order.id());
            orders.complete(order.id(),user);
        }
        return order.id();
    }
    private SupportService.Request request(String order,Type type,int quantity) {
        return new SupportService.Request(order,item,quantity,type,"测试申请");
    }
    @Test void databasePrecisionMustNotMoveInvalidationIntoTheFuture() {
        var instant=java.time.Instant.parse("2026-09-19T00:00:00.123456700Z");
        var rounded=jdbc.queryForObject("SELECT CAST(? AS TIMESTAMP(6))",java.sql.Timestamp.class,java.sql.Timestamp.from(instant)).toInstant();
        assertThat(rounded).isAfter(instant); // deterministic reproduction, no sleeps
        var fixed=new SupportService(jdbc,new com.fasterxml.jackson.databind.ObjectMapper(),true,
                java.time.Clock.fixed(instant,java.time.ZoneOffset.UTC));
        var persisted=jdbc.queryForObject("SELECT CAST(? AS TIMESTAMP(6))",java.sql.Timestamp.class,
                java.sql.Timestamp.from(fixed.businessNow("test"))).toInstant();
        assertThat(persisted).isBeforeOrEqualTo(instant);
        assertThat(persisted).isEqualTo(fixed.businessNow("test"));
    }
    @Test void confirmedReceiptCanApplyWithIntegerQuoteAndDurableClaim() {
        String order=paid(true);
        var quote=support.preview(request(order,Type.RETURN_REFUND,2),user);
        assertThat(quote.amountMinor()).isEqualTo(202);
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM support_order_claim WHERE order_id=?",Integer.class,order)).isZero();
        var created=support.confirm(quote.previewId(),user,"apply");
        assertThat(created.phase()).isEqualTo(AfterSaleLifecycle.Phase.AWAITING_RETURN);
        assertThat(jdbc.queryForObject("SELECT quantity FROM support_order_claim WHERE order_id=?",Integer.class,order)).isEqualTo(2);
        assertThat(support.events(created.id(),user)).hasSize(1);
        assertThat(inventory.getStock("PRODUCT",item).availableQuantity()).isEqualTo(17);
    }
    @Test void independentConsoleReadsAndProcessesRequireAdmin() throws Exception {
        String order=paid(true);var card=support.preview(request(order,Type.REFUND_ONLY,1),user);
        var current=support.confirm(card.previewId(),user,"console-confirm");
        http.perform(get("/api/admin/support-simulator/cases/{id}",current.id()).with(jwt().jwt(j->j.subject(user))))
                .andExpect(status().isForbidden());
        http.perform(get("/api/admin/support-simulator/cases/{id}",current.id()).with(jwt().jwt(j->j.subject("operator"))
                .authorities(new org.springframework.security.core.authority.SimpleGrantedAuthority("ROLE_ADMIN"))))
                .andExpect(status().isOk()).andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath("$.data.case.id").value(current.id()));
        http.perform(org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post("/api/admin/support-simulator/cases/{id}/process",current.id())
                .with(jwt().jwt(j->j.subject(user)))).andExpect(status().isForbidden());
        assertThat(support.get(current.id(),user).phase()).isEqualTo(AfterSaleLifecycle.Phase.AWAITING_REVIEW);
    }
    @Test void confirmationReplayDoesNotDuplicateEvenAfterCardExpires() {
        String order=paid(true);var quote=support.preview(request(order,Type.REFUND_ONLY,1),user);
        var first=support.confirm(quote.previewId(),user,"same");
        jdbc.update("UPDATE support_preview SET expires_at=? WHERE id=?",java.sql.Timestamp.from(java.time.Instant.now().minusSeconds(60)),quote.previewId());
        assertThat(support.confirm(quote.previewId(),user,"same").id()).isEqualTo(first.id());
        assertThatThrownBy(()->support.confirm(quote.previewId(),user,"other")).isInstanceOf(BusinessConflictException.class);
        assertThat(support.list(order,user)).hasSize(1);
    }
    @Test void oldCardCannotConfirmChangedFactsOrReuseKeyForAnotherPreview() {
        String order=paid(true);var one=support.preview(request(order,Type.REFUND_ONLY,1),user);
        var two=support.preview(request(order,Type.REFUND_ONLY,2),user);
        assertThatThrownBy(()->support.confirm(one.previewId(),user,"old")).isInstanceOf(BusinessConflictException.class).hasMessageContaining("过期");
        var created=support.confirm(two.previewId(),user,"key");
        support.cancel(created.id(),user,0,"cancel");
        var three=support.preview(request(order,Type.REFUND_ONLY,1),user);
        assertThatThrownBy(()->support.confirm(three.previewId(),user,"key")).isInstanceOf(BusinessConflictException.class).hasMessageContaining("幂等键");
        jdbc.update("UPDATE customer_order SET version=version+1 WHERE id=?",order);
        assertThatThrownBy(()->support.confirm(three.previewId(),user,"different")).isInstanceOf(BusinessConflictException.class).hasMessageContaining("变化");
    }
    @Test void expiredCardAndOtherOwnerCannotWrite() {
        String order=paid(true);var quote=support.preview(request(order,Type.RETURN_REFUND,1),user);
        assertThatThrownBy(()->support.confirm(quote.previewId(),"stranger","bad")).isInstanceOf(ResourceNotFoundException.class);
        jdbc.update("UPDATE support_preview SET expires_at=? WHERE id=?",java.sql.Timestamp.from(java.time.Instant.now().minusSeconds(60)),quote.previewId());
        assertThatThrownBy(()->support.confirm(quote.previewId(),user,"late")).isInstanceOf(BusinessConflictException.class).hasMessageContaining("过期");
        assertThat(support.list(order,user)).isEmpty();
    }
    @Test void missingSpecificationAllowsReturnButBlocksAutomaticExchange() {
        String order=paid(true);jdbc.update("UPDATE order_item SET evidence_json='{}' WHERE order_id=?",order);
        assertThatThrownBy(()->support.preview(request(order,Type.EXCHANGE,1),user)).isInstanceOf(BusinessConflictException.class)
                .hasMessageContaining("MISSING_PURCHASE_SPECIFICATION");
        assertThat(support.preview(request(order,Type.RETURN_REFUND,1),user).amountMinor()).isEqualTo(101);
    }
    @Test void cancellationReleasesClaimOnceAndDoesNotMoveMoneyOrStock() {
        String order=paid(true);var quote=support.preview(request(order,Type.EXCHANGE,1),user);
        var created=support.confirm(quote.previewId(),user,"create");
        assertThatThrownBy(()->support.cancel(created.id(),user,9,"wrong-version")).isInstanceOf(BusinessConflictException.class);
        assertThat(support.cancel(created.id(),user,0,"cancel").phase()).isEqualTo(AfterSaleLifecycle.Phase.CANCELLED);
        assertThat(support.cancel(created.id(),user,0,"cancel").phase()).isEqualTo(AfterSaleLifecycle.Phase.CANCELLED);
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM support_order_claim WHERE order_id=?",Integer.class,order)).isZero();
        assertThat(support.events(created.id(),user)).hasSize(2);
        assertThat(jdbc.queryForObject("SELECT refunded_minor FROM order_line_allocation WHERE order_id=?",Long.class,order)).isZero();
        assertThat(support.preview(request(order,Type.RETURN_REFUND,3),user).amountMinor()).isEqualTo(303);
    }
    @Test void activeNewCaseAlsoBlocksLegacyQuantityRefundEntry() {
        String order=paid(true);var quote=support.preview(request(order,Type.REFUND_ONLY,1),user);
        support.confirm(quote.previewId(),user,"create");
        assertThatThrownBy(()->oldRefunds.create(order,user,"legacy",new PartialRefundRequest(List.of(new PartialRefundRequest.Line(item,1)),"old",null)))
                .isInstanceOf(BusinessConflictException.class).hasMessageContaining("售后处理中");
    }
    @Test void simultaneousDistinctKeysCanClaimOnlyOneCase() throws Exception {
        String order=paid(true);var a=support.preview(request(order,Type.EXCHANGE,1),user);var b=a;
        ExecutorService pool=Executors.newFixedThreadPool(2);CountDownLatch start=new CountDownLatch(1);
        try {
            Callable<Boolean> first=()->{start.await();try{support.confirm(a.previewId(),user,"a");return true;}catch(BusinessConflictException expected){return false;}};
            Callable<Boolean> second=()->{start.await();try{support.confirm(b.previewId(),user,"b");return true;}catch(BusinessConflictException expected){return false;}};
            var fa=pool.submit(first);var fb=pool.submit(second);start.countDown();
            assertThat(List.of(fa.get(10,TimeUnit.SECONDS),fb.get(10,TimeUnit.SECONDS))).containsExactlyInAnyOrder(true,false);
            assertThat(support.list(order,user)).hasSize(1);
            assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM support_order_claim WHERE order_id=?",Integer.class,order)).isEqualTo(1);
        } finally {pool.shutdownNow();}
    }
    @Test void supportReadEndpointsRequireAuthenticationAndOwnership() throws Exception {
        String order=paid(true);var quote=support.preview(request(order,Type.REFUND_ONLY,1),user);
        var created=support.confirm(quote.previewId(),user,"create");
        http.perform(get("/api/after-sales/{id}",created.id())).andExpect(status().isUnauthorized());
        http.perform(get("/api/after-sales/{id}",created.id()).with(jwt().jwt(j->j.subject("stranger")))).andExpect(status().isNotFound());
        http.perform(get("/api/after-sales/{id}",created.id()).with(jwt().jwt(j->j.subject(user)))).andExpect(status().isOk());
    }
    @Test void overQuantityAndUnshippedPathDoNotCreateSupportCases() {
        String order=paid(true);
        assertThatThrownBy(()->support.preview(request(order,Type.REFUND_ONLY,4),user)).isInstanceOf(BusinessConflictException.class);
        String unshipped=paid(false);
        assertThatThrownBy(()->support.preview(request(unshipped,Type.REFUND_ONLY,1),user)).isInstanceOf(BusinessConflictException.class)
                .hasMessageContaining("USE_EXISTING_UNSHIPPED_REFUND");
    }
    @Test void explicitSpecificationIsFrozenInPurchaseEvidence() throws Exception {
        jdbc.update("INSERT INTO support_sale_specification(product_id,code,label,version) VALUES(?,?,?,1)",item,"black-256","黑色 256GB");
        String snapshot=specifications.enrich(item,"{\"source\":\"fixture\"}");
        var json=new com.fasterxml.jackson.databind.ObjectMapper();
        assertThat(json.readTree(snapshot).path("saleSpecification").path("code").asText()).isEqualTo("black-256");
        assertThat(json.readTree(snapshot).path("supportPolicyVersion").asText()).isEqualTo(AfterSalePolicy.VERSION);
        jdbc.update("UPDATE support_sale_specification SET code='white-128',version=2 WHERE product_id=?",item);
        assertThat(json.readTree(snapshot).path("saleSpecification").path("code").asText()).isEqualTo("black-256");
        assertThat(json.readTree(specifications.enrich(item+999999,"{}")).has("saleSpecification")).isFalse();
    }
    @Test void disabledWritesDoNotCreatePreviewOrClaim() {
        String order=paid(true);
        var disabled=new SupportService(jdbc,new com.fasterxml.jackson.databind.ObjectMapper(),false);
        assertThatThrownBy(()->disabled.preview(request(order,Type.REFUND_ONLY,1),user)).isInstanceOf(ForbiddenOperationException.class);
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM support_preview WHERE order_id=?",Integer.class,order)).isZero();
    }
    private SupportService.CaseView applyCase(String order,Type type,int quantity) {
        return support.confirm(support.preview(request(order,type,quantity),user).previewId(),user,"apply");
    }
    private SupportReceiptSimulator.Receipt receipt(SupportService.CaseView current,AfterSaleLifecycle.Event event,
                                                    Long receivedItem,Integer quantity,Boolean sellable,String key) {
        return simulator.record(current.id(),new SupportReceiptSimulator.Input(event,current.version(),receivedItem,quantity,sellable,"模拟审核证据"),"operator",key);
    }
    @Test void ownerCanReadPendingReceiptWithoutApplyingItOrExposingInternalFields() throws Exception {
        var current=applyCase(paid(true),Type.REFUND_ONLY,1);
        var pending=receipt(current,AfterSaleLifecycle.Event.APPROVE,null,null,null,"public-receipt");
        var rows=support.receipts(current.id(),user);
        assertThat(rows).hasSize(1);
        assertThat(rows.get(0).id()).isEqualTo(pending.id());
        assertThat(rows.get(0).status()).isEqualTo("PENDING");
        assertThat(rows.get(0).appliedAt()).isNull();
        assertThat(support.get(current.id(),user).phase()).isEqualTo(AfterSaleLifecycle.Phase.AWAITING_REVIEW);
        assertThatThrownBy(()->support.receipts(current.id(),"other-user")).isInstanceOf(ResourceNotFoundException.class);
        http.perform(get("/api/after-sales/{id}/receipts",current.id()).with(jwt().jwt(j->j.subject(user))))
                .andExpect(status().isOk())
                .andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath("$.data[0].status").value("PENDING"))
                .andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath("$.data[0].payload").doesNotExist())
                .andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath("$.data[0].actor").doesNotExist());
        http.perform(get("/api/after-sales/{id}/receipts",current.id()).with(jwt().jwt(j->j.subject("other-user"))))
                .andExpect(status().isNotFound());
    }
    @Test void refundOnlyRequiresIndependentApprovalAndProviderReceiptAndNeverRestocks() {
        String order=paid(true);var current=applyCase(order,Type.REFUND_ONLY,2);
        var approval=receipt(current,AfterSaleLifecycle.Event.APPROVE,null,null,null,"approval");
        assertThat(support.get(current.id(),user).phase()).isEqualTo(AfterSaleLifecycle.Phase.AWAITING_REVIEW);
        current=operations.reconcile(approval.id(),user);
        assertThat(current.phase()).isEqualTo(AfterSaleLifecycle.Phase.REFUND_PENDING);
        var provider=simulator.refundSucceeded(current.id(),"operator","provider");
        assertThat(support.get(current.id(),user).phase()).isEqualTo(AfterSaleLifecycle.Phase.REFUND_PENDING);
        var done=operations.reconcile(provider.id(),user);
        assertThat(done.phase()).isEqualTo(AfterSaleLifecycle.Phase.COMPLETED);
        assertThat(operations.reconcile(provider.id(),user).version()).isEqualTo(done.version());
        assertThat(simulator.refundSucceeded(current.id(),"operator","provider").id()).isEqualTo(provider.id());
        assertThat(jdbc.queryForObject("SELECT refunded_minor FROM order_line_allocation WHERE order_id=?",Long.class,order)).isEqualTo(202);
        assertThat(jdbc.queryForObject("SELECT refunded_quantity FROM order_line_allocation WHERE order_id=?",Integer.class,order)).isEqualTo(2);
        assertThat(inventory.getStock("PRODUCT",item).availableQuantity()).isEqualTo(17);
        assertThat(jdbc.queryForObject("SELECT status FROM customer_order WHERE id=?",String.class,order)).isEqualTo("COMPLETED");
        assertThat(jdbc.queryForObject("SELECT status FROM fulfillment_task WHERE order_id=?",String.class,order)).isEqualTo("RECEIVED");
        assertThatThrownBy(()->support.preview(request(order,Type.REFUND_ONLY,2),user)).isInstanceOf(BusinessConflictException.class);
        assertThat(support.preview(request(order,Type.REFUND_ONLY,1),user).amountMinor()).isEqualTo(101);
    }
    @Test void badProviderAmountRollsBackAllSettlementWrites() {
        String order=paid(true);var current=applyCase(order,Type.REFUND_ONLY,1);
        operations.reconcile(receipt(current,AfterSaleLifecycle.Event.APPROVE,null,null,null,"approve").id(),user);
        var provider=simulator.refundSucceeded(current.id(),"operator","refund");
        jdbc.update("UPDATE support_receipt SET payload_json=? WHERE id=?",provider.payload().replace("\"amountMinor\":101","\"amountMinor\":102"),provider.id());
        assertThatThrownBy(()->operations.reconcile(provider.id(),user)).isInstanceOf(BusinessConflictException.class).hasMessageContaining("回执");
        assertThat(jdbc.queryForObject("SELECT refunded_minor FROM order_line_allocation WHERE order_id=?",Long.class,order)).isZero();
        assertThat(jdbc.queryForObject("SELECT status FROM support_refund_command WHERE case_id=?",String.class,current.id())).isEqualTo("PENDING");
        assertThat(simulator.get(provider.id()).status()).isEqualTo("PENDING");
    }
    @Test void rejectedReviewReleasesHoldWithoutRefundCommand() {
        String order=paid(true);var current=applyCase(order,Type.REFUND_ONLY,1);
        var rejected=operations.reconcile(receipt(current,AfterSaleLifecycle.Event.REJECT,null,null,null,"reject").id(),user);
        assertThat(rejected.phase()).isEqualTo(AfterSaleLifecycle.Phase.REJECTED);
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM support_order_claim WHERE order_id=?",Integer.class,order)).isZero();
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM support_refund_command WHERE case_id=?",Integer.class,current.id())).isZero();
    }
    @Test void exhaustedReceiptStopsAutomaticRetriesButOriginalReceiptCanBeManuallyApplied() {
        String order=paid(true);var current=applyCase(order,Type.REFUND_ONLY,1);
        var approval=receipt(current,AfterSaleLifecycle.Event.APPROVE,null,null,null,"stable-approval");
        jdbc.update("UPDATE support_receipt SET attempts=8 WHERE id=?",approval.id());
        recovery.recover(1);
        assertThat(simulator.get(approval.id()).status()).isEqualTo("NEEDS_REVIEW");
        assertThat(support.get(current.id(),user).phase()).isEqualTo(AfterSaleLifecycle.Phase.AWAITING_REVIEW);
        assertThat(jdbc.queryForObject("SELECT refunded_minor FROM order_line_allocation WHERE order_id=?",Long.class,order)).isZero();
        var applied=operations.reconcile(approval.id(),user);
        assertThat(applied.phase()).isEqualTo(AfterSaleLifecycle.Phase.REFUND_PENDING);
        assertThat(operations.reconcile(approval.id(),user).version()).isEqualTo(applied.version());
        assertThat(simulator.get(approval.id()).status()).isEqualTo("APPLIED");
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM support_receipt WHERE case_id=?",Integer.class,current.id())).isEqualTo(1);
    }
    @Test void reviewedTicketOnlyReopensWarehouseCheckAndRequiresFreshReceipt() {
        String order=paid(true);var current=applyCase(order,Type.RETURN_REFUND,1);
        current=operations.submitReturn(current.id(),user,current.version(),"RETURN-REVIEW","return");
        var wrong=receipt(current,AfterSaleLifecycle.Event.RETURN_RECEIVED,item+1,1,null,"wrong-item");
        current=operations.reconcile(wrong.id(),user);String id=current.id();long version=current.version();
        var ticket=tickets.create(new SupportTickets.Request(order,id,SupportTickets.Category.INFO_VERIFY,"核对退货识别错误"),user,"review-ticket");
        assertThatThrownBy(()->operations.resumeReview(id,user,version,ticket.id(),"operator","resume")).isInstanceOf(BusinessConflictException.class);
        tickets.administer(ticket.id(),"operator",ticket.version(),"RESOLVE","已核对仓库应重新扫描商品身份","resolved");
        assertThat(support.get(id,user).phase()).isEqualTo(AfterSaleLifecycle.Phase.NEEDS_REVIEW);
        var reopened=operations.resumeReview(id,user,version,ticket.id(),"operator","resume");
        assertThat(reopened.phase()).isEqualTo(AfterSaleLifecycle.Phase.RETURN_IN_TRANSIT);
        assertThat(operations.resumeReview(id,user,version,ticket.id(),"operator","resume").version()).isEqualTo(reopened.version());
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM support_order_claim WHERE case_id=?",Integer.class,id)).isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM support_refund_command WHERE case_id=?",Integer.class,id)).isZero();
        assertThat(simulator.get(wrong.id()).status()).isEqualTo("APPLIED");
        var corrected=receipt(reopened,AfterSaleLifecycle.Event.RETURN_RECEIVED,item,1,null,"new-scan");
        assertThat(operations.reconcile(corrected.id(),user).phase()).isEqualTo(AfterSaleLifecycle.Phase.AWAITING_INSPECTION);
    }
    @Test void returnTrackingReceiptAndInspectionAreDistinctAndDisposalIsDurable() {
        String order=paid(true);var current=applyCase(order,Type.RETURN_REFUND,1);
        current=operations.submitReturn(current.id(),user,current.version(),"TRACK-01","track");
        assertThat(operations.submitReturn(current.id(),user,0,"TRACK-01","track").version()).isEqualTo(current.version());
        var received=receipt(current,AfterSaleLifecycle.Event.RETURN_RECEIVED,item,1,null,"received");
        current=operations.reconcile(received.id(),user);
        assertThat(current.phase()).isEqualTo(AfterSaleLifecycle.Phase.AWAITING_INSPECTION);
        var inspection=receipt(current,AfterSaleLifecycle.Event.INSPECTION_ACCEPTED,item,1,false,"inspection");
        current=operations.reconcile(inspection.id(),user);
        assertThat(current.phase()).isEqualTo(AfterSaleLifecycle.Phase.REFUND_PENDING);
        assertThat(jdbc.queryForObject("SELECT kind FROM support_stock_effect WHERE case_id=?",String.class,current.id())).isEqualTo("RETURN_QUARANTINE");
        assertThat(inventory.getStock("PRODUCT",item).availableQuantity()).isEqualTo(17);
        var provider=simulator.refundSucceeded(current.id(),"operator","provider");
        assertThatThrownBy(()->operations.reconcile(provider.id(),user)).isInstanceOf(BusinessConflictException.class).hasMessageContaining("库存处置尚未确认");
        String effect=jdbc.queryForObject("SELECT effect_id FROM support_stock_effect WHERE case_id=?",String.class,current.id());
        assertThat(stockEffects.apply(effect)).isTrue();
        assertThat(stockEffects.apply(effect)).isTrue();
        assertThat(inventory.getStock("PRODUCT",item).availableQuantity()).isEqualTo(17);
        assertThat(jdbc.queryForObject("SELECT total_quantity FROM inventory_stock WHERE item_id=?",Integer.class,item)).isEqualTo(19);
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM inventory_return_receipt WHERE command_id=?",Integer.class,effect)).isEqualTo(1);
        assertThat(operations.reconcile(provider.id(),user).phase()).isEqualTo(AfterSaleLifecycle.Phase.COMPLETED);
    }
    @Test void sellableReturnRecoveryRestocksExactlyOnceAndCompletesRefund() {
        String order=paid(true);var current=applyCase(order,Type.RETURN_REFUND,3);
        current=operations.submitReturn(current.id(),user,current.version(),"TRACK-FULL","track");
        current=operations.reconcile(receipt(current,AfterSaleLifecycle.Event.RETURN_RECEIVED,item,3,null,"received").id(),user);
        current=operations.reconcile(receipt(current,AfterSaleLifecycle.Event.INSPECTION_ACCEPTED,item,3,true,"inspection").id(),user);
        var provider=simulator.refundSucceeded(current.id(),"operator","provider");
        recovery.recover(100);
        assertThat(support.get(current.id(),user).phase()).isEqualTo(AfterSaleLifecycle.Phase.COMPLETED);
        assertThat(operations.reconcile(provider.id(),user).phase()).isEqualTo(AfterSaleLifecycle.Phase.COMPLETED);
        recovery.recover(100);
        assertThat(inventory.getStock("PRODUCT",item).availableQuantity()).isEqualTo(20);
        assertThat(jdbc.queryForObject("SELECT status FROM inventory_reservation WHERE order_id=?",String.class,order)).isEqualTo("RETURNED");
        assertThat(jdbc.queryForObject("SELECT returned_quantity FROM inventory_reservation WHERE order_id=?",Integer.class,order)).isEqualTo(3);
        assertThat(jdbc.queryForObject("SELECT refunded_minor FROM order_line_allocation WHERE order_id=?",Long.class,order)).isEqualTo(303);
    }
    private SupportService.CaseView inspectedExchange(String order) {
        var current=applyCase(order,Type.EXCHANGE,1);
        current=operations.submitReturn(current.id(),user,current.version(),"EXCHANGE-TRACK","track");
        current=operations.reconcile(receipt(current,AfterSaleLifecycle.Event.RETURN_RECEIVED,item,1,null,"received").id(),user);
        return operations.reconcile(receipt(current,AfterSaleLifecycle.Event.INSPECTION_ACCEPTED,item,1,false,"inspection").id(),user);
    }
    @Test void expiredReplacementReleasesExactlyOnceAndRequiresAnotherUserChoice() {
        var current=inspectedExchange(paid(true));String id=current.id();
        stockEffects.apply("return:"+id);operations.reserveReplacement(id,user);
        int before=inventory.getStock("PRODUCT",item).availableQuantity();
        jdbc.update("UPDATE support_replacement SET reserve_until=? WHERE case_id=?",java.sql.Timestamp.from(java.time.Instant.now().minusSeconds(1)),id);
        assertThatThrownBy(()->simulator.replacementEvent(id,false,"LATE-TRACK","operator","late-before-release"))
                .isInstanceOf(BusinessConflictException.class).hasMessageContaining("预占期限");
        var expired=operations.expireReplacement(id,user);
        assertThat(expired.phase()).isEqualTo(AfterSaleLifecycle.Phase.WAITING_CHOICE);
        assertThat(inventory.getStock("PRODUCT",item).availableQuantity()).isEqualTo(before+1);
        assertThat(inventory.getStock("PRODUCT",item).reservedQuantity()).isZero();
        assertThat(operations.expireReplacement(id,user).version()).isEqualTo(expired.version());
        assertThat(inventory.getStock("PRODUCT",item).availableQuantity()).isEqualTo(before+1);
        assertThatThrownBy(()->simulator.replacementEvent(id,false,"LATE-TRACK","operator","late-dispatch")).isInstanceOf(BusinessConflictException.class);
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM support_order_claim WHERE case_id=?",Integer.class,id)).isEqualTo(1);
        var conversion=operations.previewConversion(id,user);
        assertThat(conversion.amountMinor()).isEqualTo(101);
    }
    @Test void independentDispatchReceiptPreventsExpiredReservationReleaseBeforeLocalAck() {
        var current=inspectedExchange(paid(true));String id=current.id();
        stockEffects.apply("return:"+id);operations.reserveReplacement(id,user);
        var dispatch=simulator.replacementEvent(id,false,"BEFORE-TIMEOUT","operator","dispatch");
        jdbc.update("UPDATE support_replacement SET reserve_until=? WHERE case_id=?",java.sql.Timestamp.from(java.time.Instant.now().minusSeconds(1)),id);
        assertThat(simulator.replacementEvent(id,false,"BEFORE-TIMEOUT","operator","dispatch").id()).isEqualTo(dispatch.id());
        assertThat(operations.expireReplacement(id,user).phase()).isEqualTo(AfterSaleLifecycle.Phase.REPLACEMENT_READY);
        assertThat(inventory.getStock("PRODUCT",item).reservedQuantity()).isEqualTo(1);
        assertThat(operations.reconcile(dispatch.id(),user).phase()).isEqualTo(AfterSaleLifecycle.Phase.REPLACEMENT_SHIPPED);
        assertThat(operations.expireReplacement(id,user).phase()).isEqualTo(AfterSaleLifecycle.Phase.REPLACEMENT_SHIPPED);
    }
    @Test void exchangeConversionNeedsOwnedFreshPreviewAndConfirmedChange() {
        String order=paid(true);var current=inspectedExchange(order);
        assertThat(current.phase()).isEqualTo(AfterSaleLifecycle.Phase.WAITING_STOCK);
        var preview=operations.previewConversion(current.id(),user);
        assertThat(support.get(current.id(),user).type()).isEqualTo(Type.EXCHANGE);
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM support_refund_command WHERE case_id=?",Integer.class,current.id())).isZero();
        assertThatThrownBy(()->operations.confirmConversion(preview.previewId(),"other-user","convert")).isInstanceOf(ResourceNotFoundException.class);
        current=operations.confirmConversion(preview.previewId(),user,"convert");
        assertThat(current.type()).isEqualTo(Type.RETURN_REFUND);
        assertThat(current.phase()).isEqualTo(AfterSaleLifecycle.Phase.REFUND_PENDING);
        assertThat(current.amountMinor()).isEqualTo(101);
        assertThat(operations.confirmConversion(preview.previewId(),user,"convert").version()).isEqualTo(current.version());
        assertThatThrownBy(()->operations.confirmConversion(preview.previewId(),user,"another-key")).isInstanceOf(BusinessConflictException.class);
        var refund=simulator.refundSucceeded(current.id(),"operator","refund");
        stockEffects.apply("return:"+current.id());
        assertThat(operations.reconcile(refund.id(),user).phase()).isEqualTo(AfterSaleLifecycle.Phase.COMPLETED);
        assertThat(inventory.getStock("PRODUCT",item).availableQuantity()).isEqualTo(17);
        assertThat(jdbc.queryForObject("SELECT refunded_minor FROM order_line_allocation WHERE order_id=?",Long.class,order)).isEqualTo(101);
    }
    @Test void conversionRejectsReplacedExpiredAndVersionChangedCards() {
        var current=inspectedExchange(paid(true));
        var old=operations.previewConversion(current.id(),user);
        var fresh=operations.previewConversion(current.id(),user);
        assertThatThrownBy(()->operations.confirmConversion(old.previewId(),user,"old")).isInstanceOf(BusinessConflictException.class).hasMessageContaining("过期");
        jdbc.update("UPDATE support_conversion_preview SET expires_at=? WHERE id=?",java.sql.Timestamp.from(java.time.Instant.now().minusSeconds(1)),fresh.previewId());
        assertThatThrownBy(()->operations.confirmConversion(fresh.previewId(),user,"expired")).isInstanceOf(BusinessConflictException.class).hasMessageContaining("过期");
        var changed=operations.previewConversion(current.id(),user);
        // Isolated state fixture for the future inventory receipt; not a live reservation claim.
        jdbc.update("UPDATE support_case SET phase='REPLACEMENT_READY',version=version+1 WHERE id=?",current.id());
        assertThatThrownBy(()->operations.confirmConversion(changed.previewId(),user,"stale")).isInstanceOf(BusinessConflictException.class).hasMessageContaining("状态已改变");
        assertThatThrownBy(()->operations.previewConversion(current.id(),user)).isInstanceOf(BusinessConflictException.class);
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM support_refund_command WHERE case_id=?",Integer.class,current.id())).isZero();
    }
    @Test void shortageWaitUsesVersionAndIdempotencyWithoutCreatingRefund() {
        var current=inspectedExchange(paid(true));
        // State fixture only: independent stock shortage handling is implemented separately.
        jdbc.update("UPDATE support_case SET phase='WAITING_CHOICE',version=version+1 WHERE id=?",current.id());
        current=support.get(current.id(),user);String caseId=current.id();long version=current.version();
        var waited=operations.chooseWait(caseId,user,version,"wait");
        assertThat(waited.phase()).isEqualTo(AfterSaleLifecycle.Phase.WAITING_STOCK);
        assertThat(operations.chooseWait(caseId,user,version,"wait").version()).isEqualTo(waited.version());
        assertThatThrownBy(()->operations.chooseWait(caseId,user,version,"another")).isInstanceOf(BusinessConflictException.class);
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM support_refund_command WHERE case_id=?",Integer.class,caseId)).isZero();
    }
    @Test void replacementReservesOnlyAfterInspectionDisposalAndNeverTwice() {
        String order=paid(true);
        jdbc.update("UPDATE order_item SET evidence_json=? WHERE order_id=?",
                "{\"saleSpecification\":{\"code\":\"black-256\",\"label\":\""+"规".repeat(300)+"\"}}",order);
        var requested=applyCase(order,Type.EXCHANGE,1);
        assertThat(operations.reserveReplacement(requested.id(),user).phase()).isEqualTo(AfterSaleLifecycle.Phase.AWAITING_RETURN);
        assertThat(inventory.getStock("PRODUCT",item).availableQuantity()).isEqualTo(17);
        var current=operations.submitReturn(requested.id(),user,requested.version(),"REPLACE-TRACK","track");
        current=operations.reconcile(receipt(current,AfterSaleLifecycle.Event.RETURN_RECEIVED,item,1,null,"received").id(),user);
        current=operations.reconcile(receipt(current,AfterSaleLifecycle.Event.INSPECTION_ACCEPTED,item,1,false,"inspection").id(),user);
        String id=current.id();
        var card=operations.previewConversion(id,user);
        assertThat(operations.reserveReplacement(id,user).phase()).isEqualTo(AfterSaleLifecycle.Phase.WAITING_STOCK);
        stockEffects.apply("return:"+id);
        assertThat(operations.reserveReplacement(id,user).phase()).isEqualTo(AfterSaleLifecycle.Phase.REPLACEMENT_READY);
        operations.reserveReplacement(id,user);
        assertThat(inventory.getStock("PRODUCT",item).availableQuantity()).isEqualTo(16);
        assertThat(inventory.getStock("PRODUCT",item).reservedQuantity()).isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM support_replacement WHERE case_id=?",Integer.class,id)).isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT specification FROM support_replacement WHERE case_id=?",String.class,id)).isEqualTo(requested.specification());
        assertThat(jdbc.queryForObject("SELECT quantity FROM inventory_reservation WHERE order_id=?",Integer.class,order)).isEqualTo(3);
        assertThatThrownBy(()->operations.confirmConversion(card.previewId(),user,"late-convert")).isInstanceOf(BusinessConflictException.class);
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM payment_record WHERE order_id=?",Integer.class,order)).isEqualTo(1);
    }
    @Test void actualShortageAllowsWaitThenFreshAttemptAfterStockReturns() {
        String order=paid(true);String competing=UUID.randomUUID().toString();
        inventory.reserve(competing,"PRODUCT",item,17,java.time.LocalDateTime.now().plusHours(1));
        var current=inspectedExchange(order);String id=current.id();
        stockEffects.apply("return:"+id);
        var shortage=operations.reserveReplacement(id,user);
        assertThat(shortage.phase()).isEqualTo(AfterSaleLifecycle.Phase.WAITING_CHOICE);
        assertThat(jdbc.queryForObject("SELECT status FROM support_replacement WHERE case_id=?",String.class,id)).isEqualTo("SHORTAGE");
        operations.previewConversion(id,user);
        operations.chooseWait(id,user,shortage.version(),"wait-stock");
        inventory.release(competing,false);
        assertThat(operations.reserveReplacement(id,user).phase()).isEqualTo(AfterSaleLifecycle.Phase.REPLACEMENT_READY);
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM support_replacement WHERE case_id=?",Integer.class,id)).isEqualTo(2);
        assertThat(inventory.getStock("PRODUCT",item).reservedQuantity()).isEqualTo(1);
    }
    @Test void replacementDispatchAndReceiptCompleteWithoutNewPaymentOrDuplicateStock() {
        String order=paid(true);var current=inspectedExchange(order);String id=current.id();
        stockEffects.apply("return:"+id);operations.reserveReplacement(id,user);
        assertThatThrownBy(()->simulator.replacementEvent(id,true,"REPLACE-001","operator","too-early")).isInstanceOf(BusinessConflictException.class);
        var dispatched=simulator.replacementEvent(id,false,"REPLACE-001","operator","dispatch");
        assertThatThrownBy(()->simulator.replacementEvent(id,false,"REPLACE-OTHER","operator","different-key")).isInstanceOf(BusinessConflictException.class);
        assertThat(support.get(id,user).phase()).isEqualTo(AfterSaleLifecycle.Phase.REPLACEMENT_READY);
        assertThat(operations.reconcile(dispatched.id(),user).phase()).isEqualTo(AfterSaleLifecycle.Phase.REPLACEMENT_SHIPPED);
        operations.reconcile(dispatched.id(),user);
        assertThat(simulator.replacementEvent(id,false,"REPLACE-001","operator","dispatch").id()).isEqualTo(dispatched.id());
        assertThat(inventory.getStock("PRODUCT",item).reservedQuantity()).isZero();
        assertThat(inventory.getStock("PRODUCT",item).availableQuantity()).isEqualTo(16);
        assertThat(inventory.getStock("PRODUCT",item).soldQuantity()).isEqualTo(3);
        assertThatThrownBy(()->simulator.replacementEvent(id,true,"WRONG-TRACK","operator","wrong")).isInstanceOf(BusinessConflictException.class);
        var received=simulator.replacementEvent(id,true,"REPLACE-001","operator","received-replacement");
        assertThatThrownBy(()->simulator.replacementEvent(id,true,"REPLACE-001","operator","received-different-key")).isInstanceOf(BusinessConflictException.class);
        assertThat(operations.reconcile(received.id(),user).phase()).isEqualTo(AfterSaleLifecycle.Phase.COMPLETED);
        operations.reconcile(received.id(),user);
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM support_order_claim WHERE order_id=?",Integer.class,order)).isZero();
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM payment_record WHERE order_id=?",Integer.class,order)).isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT refunded_minor FROM order_line_allocation WHERE order_id=?",Long.class,order)).isZero();
        assertThatThrownBy(()->support.preview(request(order,Type.REFUND_ONLY,3),user)).isInstanceOf(BusinessConflictException.class);
        assertThat(support.preview(request(order,Type.REFUND_ONLY,2),user).amountMinor()).isEqualTo(202);
    }
    @Test void forgedReplacementReceiptCannotMoveStockOrCompleteCase() {
        var current=inspectedExchange(paid(true));String id=current.id();
        stockEffects.apply("return:"+id);operations.reserveReplacement(id,user);
        var dispatch=simulator.replacementEvent(id,false,"REPLACE-002","operator","dispatch");
        String changed=dispatch.payload().replace("\"chargeMinor\":0","\"chargeMinor\":1");
        jdbc.update("UPDATE support_receipt SET payload_json=?,request_hash=? WHERE id=?",changed,
                SupportReceiptSimulator.hash(dispatch.event()+"\n"+dispatch.expectedVersion()+"\n"+changed),dispatch.id());
        assertThatThrownBy(()->operations.reconcile(dispatch.id(),user)).isInstanceOf(BusinessConflictException.class);
        assertThat(inventory.getStock("PRODUCT",item).reservedQuantity()).isEqualTo(1);
        assertThat(support.get(id,user).phase()).isEqualTo(AfterSaleLifecycle.Phase.REPLACEMENT_READY);
    }
    @Test void unresolvedReplacementBlocksConversionWithoutPretendingShortage() {
        var current=inspectedExchange(paid(true));String id=current.id();
        var card=operations.previewConversion(id,user);
        jdbc.update("INSERT INTO support_replacement(id,case_id,case_version,item_id,quantity,specification,reserve_until,created_at) VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                UUID.randomUUID().toString(),id,current.version(),item,1,current.specification());
        assertThatThrownBy(()->operations.previewConversion(id,user)).isInstanceOf(BusinessConflictException.class).hasMessageContaining("待核实");
        assertThatThrownBy(()->operations.confirmConversion(card.previewId(),user,"convert")).isInstanceOf(BusinessConflictException.class);
        assertThat(support.get(id,user).phase()).isEqualTo(AfterSaleLifecycle.Phase.WAITING_STOCK);
    }
    @Test void wrongReturnedItemBecomesReviewAndCannotCreateRefundCommand() {
        String order=paid(true);var current=applyCase(order,Type.RETURN_REFUND,1);
        current=operations.submitReturn(current.id(),user,0,"TRACK-02","track");
        var mismatch=receipt(current,AfterSaleLifecycle.Event.RETURN_RECEIVED,item+1,1,null,"mismatch");
        var review=operations.reconcile(mismatch.id(),user);
        assertThat(review.phase()).isEqualTo(AfterSaleLifecycle.Phase.NEEDS_REVIEW);
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM support_refund_command WHERE case_id=?",Integer.class,current.id())).isZero();
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM support_order_claim WHERE case_id=?",Integer.class,current.id())).isEqualTo(1);
    }
    @Test void originalOrderNeedsSeparateDispatchAndReceiptBeforeAftersale() {
        String order=paid(false);
        assertThatThrownBy(()->orderSimulator.record(order,true,"ORIGINAL-01","operator","early")).isInstanceOf(BusinessConflictException.class);
        var dispatch=orderSimulator.record(order,false,"ORIGINAL-01","operator","dispatch");
        assertThat(jdbc.queryForObject("SELECT status FROM fulfillment_task WHERE order_id=?",String.class,order)).isEqualTo("DISPATCHING");
        assertThatThrownBy(()->oldRefunds.create(order,user,"race",new PartialRefundRequest(List.of(new PartialRefundRequest.Line(item,1)),"race",null)))
                .isInstanceOf(BusinessConflictException.class);
        assertThat(orderSimulator.record(order,false,"ORIGINAL-01","operator","dispatch").id()).isEqualTo(dispatch.id());
        assertThatThrownBy(()->orderSimulator.record(order,false,"SECOND-TRACK","operator","different")).isInstanceOf(BusinessConflictException.class);
        orderSimulator.apply(dispatch.id());orderSimulator.apply(dispatch.id());
        assertThat(jdbc.queryForObject("SELECT status FROM customer_order WHERE id=?",String.class,order)).isEqualTo("PAID");
        assertThatThrownBy(()->orderSimulator.record(order,true,"WRONG-TRACK","operator","wrong")).isInstanceOf(BusinessConflictException.class);
        var received=orderSimulator.record(order,true,"ORIGINAL-01","operator","received");
        orderSimulator.apply(received.id());orderSimulator.apply(received.id());
        assertThat(jdbc.queryForObject("SELECT status FROM customer_order WHERE id=?",String.class,order)).isEqualTo("COMPLETED");
        assertThat(jdbc.queryForObject("SELECT status FROM fulfillment_task WHERE order_id=?",String.class,order)).isEqualTo("RECEIVED");
        assertThat(support.preview(request(order,Type.RETURN_REFUND,1),user).amountMinor()).isEqualTo(101);
        assertThat(inventory.getStock("PRODUCT",item).availableQuantity()).isEqualTo(17);
    }
    @Test void scenarioTimeIsIsolatedAndSevenDayBoundarySurvivesDelayedRecovery() {
        String order=paid(false),other=paid(false);
        var start=scenarioClock.bind(order,"operator");var otherStart=scenarioClock.bind(other,"operator");
        var dispatch=orderSimulator.record(order,false,"CLOCK-TRACK","operator","dispatch");
        recovery.recover(100);
        assertThat(orderSimulator.get(dispatch.id()).status()).isEqualTo("APPLIED");
        var receipt=orderSimulator.record(order,true,"CLOCK-TRACK","operator","received");
        scenarioClock.advance(order,0,604800,"operator","week");
        recovery.recover(100);
        assertThat(orderSimulator.get(receipt.id()).status()).isEqualTo("APPLIED");
        assertThat(jdbc.queryForObject("SELECT completed_at FROM customer_order WHERE id=?",java.time.LocalDateTime.class,order).toInstant(java.time.ZoneOffset.UTC)).isEqualTo(start.now());
        assertThat(support.preview(request(order,Type.RETURN_REFUND,1),user).amountMinor()).isEqualTo(101);
        scenarioClock.advance(order,1,1,"operator","one-second");
        assertThatThrownBy(()->support.preview(request(order,Type.RETURN_REFUND,1),user)).isInstanceOf(BusinessConflictException.class);
        assertThat(scenarioClock.advance(order,1,1,"operator","one-second").version()).isEqualTo(2);
        assertThat(scenarioClock.get(other).now()).isEqualTo(otherStart.now());
        assertThatThrownBy(()->scenarioClock.advance(order,2,-1,"operator","backwards")).isInstanceOf(InvalidBusinessStateException.class);
    }
    @Test void scenarioClockExpiresBothConfirmationTypesAndKeepsReplayStable() {
        String order=paid(false);scenarioClock.bind(order,"operator");
        orderSimulator.apply(orderSimulator.record(order,false,"TTL-TRACK","operator","dispatch").id());
        orderSimulator.apply(orderSimulator.record(order,true,"TTL-TRACK","operator","received").id());
        var expired=support.preview(request(order,Type.EXCHANGE,1),user);
        scenarioClock.advance(order,0,300,"operator","expire");
        assertThatThrownBy(()->support.confirm(expired.previewId(),user,"expired")).isInstanceOf(BusinessConflictException.class).hasMessageContaining("过期");
        var fresh=support.preview(request(order,Type.EXCHANGE,1),user);
        var current=support.confirm(fresh.previewId(),user,"accepted");
        scenarioClock.advance(order,1,300,"operator","expire-used");
        assertThat(support.confirm(fresh.previewId(),user,"accepted").id()).isEqualTo(current.id());
        current=operations.submitReturn(current.id(),user,current.version(),"TTL-RETURN","track");
        current=operations.reconcile(receipt(current,AfterSaleLifecycle.Event.RETURN_RECEIVED,item,1,null,"returned").id(),user);
        current=operations.reconcile(receipt(current,AfterSaleLifecycle.Event.INSPECTION_ACCEPTED,item,1,false,"inspect").id(),user);
        var conversion=operations.previewConversion(current.id(),user);
        scenarioClock.advance(order,2,300,"operator","expire-conversion");
        assertThatThrownBy(()->operations.confirmConversion(conversion.previewId(),user,"convert")).isInstanceOf(BusinessConflictException.class).hasMessageContaining("过期");
    }
    @Test void ticketOwnershipIdempotencyAndResolutionNeverMoveMoney() throws Exception {
        String order=paid(true);var aftersale=applyCase(order,Type.REFUND_ONLY,1);
        var request=new SupportTickets.Request(order,aftersale.id(),SupportTickets.Category.AFTERSALE_DISPUTE,"请核实审核结果");
        var ticket=tickets.create(request,user,"ticket");
        assertThat(tickets.create(request,user,"ticket").id()).isEqualTo(ticket.id());
        assertThatThrownBy(()->tickets.create(new SupportTickets.Request(order,null,SupportTickets.Category.COMPLAINT,"不同内容"),user,"ticket")).isInstanceOf(BusinessConflictException.class);
        assertThatThrownBy(()->tickets.get(ticket.id(),"other")).isInstanceOf(ResourceNotFoundException.class);
        assertThatThrownBy(()->tickets.create(request,"other","ticket")).isInstanceOf(ResourceNotFoundException.class);
        var waiting=tickets.administer(ticket.id(),"operator",0,"REQUEST_INFO","请补充描述","ask");
        assertThat(waiting.status()).isEqualTo(SupportTickets.Status.WAITING_CUSTOMER);
        var replied=tickets.reply(ticket.id(),user,waiting.version(),"已补充","reply");
        assertThat(tickets.reply(ticket.id(),user,waiting.version(),"已补充","reply").version()).isEqualTo(replied.version());
        var resolved=tickets.administer(ticket.id(),"operator",replied.version(),"RESOLVE","已说明审核规则，退款尚待审核","resolve");
        var closed=tickets.administer(ticket.id(),"operator",resolved.version(),"CLOSE","关闭本次咨询","close");
        assertThat(closed.status()).isEqualTo(SupportTickets.Status.CLOSED);
        assertThatThrownBy(()->tickets.reply(ticket.id(),user,closed.version(),"再追加","late")).isInstanceOf(BusinessConflictException.class);
        assertThat(support.get(aftersale.id(),user).phase()).isEqualTo(AfterSaleLifecycle.Phase.AWAITING_REVIEW);
        assertThat(jdbc.queryForObject("SELECT refunded_minor FROM order_line_allocation WHERE order_id=?",Long.class,order)).isZero();
        assertThat(tickets.events(ticket.id(),user)).hasSize(5);
        assertThat(tickets.list(user,0,20)).hasSize(1);
        http.perform(org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post("/api/admin/support-simulator/tickets/{id}/actions",ticket.id())
                .with(jwt().jwt(j->j.subject(user))).header("Idempotency-Key","hack").contentType("application/json")
                .content("{\"expectedVersion\":4,\"action\":\"RESOLVE\",\"message\":\"hack\"}")).andExpect(status().isForbidden());
    }
    @Test void ticketCannotBindAnotherOrderCaseOrSkipResolution() {
        String first=paid(true);var aftersale=applyCase(first,Type.REFUND_ONLY,1);String second=paid(true);
        assertThatThrownBy(()->tickets.create(new SupportTickets.Request(second,aftersale.id(),SupportTickets.Category.INFO_VERIFY,"核实"),user,"bad-link")).isInstanceOf(ResourceNotFoundException.class);
        var ticket=tickets.create(new SupportTickets.Request(second,null,SupportTickets.Category.DELIVERY_DELAY,"请催办"),user,"good");
        assertThatThrownBy(()->tickets.administer(ticket.id(),"operator",0,"CLOSE","直接关闭","skip")).isInstanceOf(BusinessConflictException.class);
        assertThatThrownBy(()->tickets.reply(ticket.id(),user,9,"陈旧请求","stale")).isInstanceOf(BusinessConflictException.class);
    }
    @Test void simulatorRejectsStaleReceiptAndCustomerCannotReachIt() throws Exception {
        String order=paid(true);var current=applyCase(order,Type.REFUND_ONLY,1);
        var approved=receipt(current,AfterSaleLifecycle.Event.APPROVE,null,null,null,"a");
        assertThatThrownBy(()->receipt(current,AfterSaleLifecycle.Event.REJECT,null,null,null,"b")).isInstanceOf(BusinessConflictException.class);
        var rejected=simulator.record(current.id(),new SupportReceiptSimulator.Input(AfterSaleLifecycle.Event.REJECT,99,null,null,null,"stale fixture"),"operator","stale");
        operations.reconcile(approved.id(),user);
        assertThatThrownBy(()->operations.reconcile(rejected.id(),user)).isInstanceOf(BusinessConflictException.class).hasMessageContaining("版本");
        http.perform(org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post("/api/admin/support-simulator/cases/{id}/refund-success",current.id())
                .with(jwt().jwt(j->j.subject(user))).header("Idempotency-Key","hack")).andExpect(status().isForbidden());
        assertThatThrownBy(()->operations.reconcile(approved.id(),"stranger")).isInstanceOf(ResourceNotFoundException.class);
    }
    @Test void receiptDrainRecoversLostRepliesAndDefersPoisonWithoutBlockingOthers() {
        String order=paid(true);var current=applyCase(order,Type.REFUND_ONLY,1);
        var poisonCase=applyCase(paid(true),Type.REFUND_ONLY,1);
        var poison=receipt(poisonCase,AfterSaleLifecycle.Event.RETURN_RECEIVED,item,1,null,"poison");
        var approval=receipt(current,AfterSaleLifecycle.Event.APPROVE,null,null,null,"approve");
        recovery.recover(100);
        assertThat(simulator.get(approval.id()).status()).isEqualTo("APPLIED");
        assertThat(simulator.get(poison.id()).status()).isEqualTo("PENDING");
        assertThat(jdbc.queryForObject("SELECT attempts FROM support_receipt WHERE id=?",Integer.class,poison.id())).isPositive();
        var provider=simulator.refundSucceeded(current.id(),"operator","provider");
        // No controller response or process-local state is needed by the next drain.
        recovery.recover(100);
        assertThat(simulator.get(provider.id()).status()).isEqualTo("APPLIED");
        assertThat(support.get(current.id(),user).phase()).isEqualTo(AfterSaleLifecycle.Phase.COMPLETED);
        assertThat(jdbc.queryForObject("SELECT refunded_minor FROM order_line_allocation WHERE order_id=?",Long.class,order)).isEqualTo(101);
    }
}
