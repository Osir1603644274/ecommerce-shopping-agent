package com.example.locallife.support;

import com.example.locallife.common.BusinessConflictException;
import com.example.locallife.inventory.InventoryService;
import com.example.locallife.inventory.remote.InventoryCommandDelivery;
import com.example.locallife.ordering.*;
import com.example.locallife.payment.PaymentService;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.test.context.*;
import org.springframework.test.context.jdbc.Sql;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import java.net.*;
import java.net.http.*;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.*;
import java.util.concurrent.Executors;
import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;
import static org.mockito.ArgumentMatchers.*;
import static com.example.locallife.support.AfterSaleLifecycle.*;

/** Two real MySQL schemas and the separately running inventory JVM; a proxy loses committed replies. */
@EnabledIfEnvironmentVariable(named="SUPPORT_REMOTE_URL",matches="http:.*")
@Sql(scripts="/support-remote-topology.sql",executionPhase=Sql.ExecutionPhase.BEFORE_TEST_CLASS)
@SpringBootTest(properties={"local-life.fulfillment.enabled=true","local-life.support.enabled=true",
        "local-life.support.simulator-enabled=true","local-life.inventory.remote-enabled=true",
        "local-life.inventory.recovery-enabled=false","local-life.support.recovery-enabled=false"})
class SupportRemoteMysqlAcceptanceIT {
    static HttpServer proxy;
    static volatile String loseCommand;
    static final java.util.concurrent.ExecutorService workers=Executors.newCachedThreadPool();
    @DynamicPropertySource static void properties(DynamicPropertyRegistry p) throws Exception {
        proxy=HttpServer.create(new InetSocketAddress("127.0.0.1",0),0);
        var wire=HttpClient.newHttpClient();
        proxy.createContext("/",exchange->{
            try {
                byte[] body=exchange.getRequestBody().readAllBytes();
                var builder=HttpRequest.newBuilder(URI.create(System.getenv("SUPPORT_REMOTE_URL")+exchange.getRequestURI()))
                        .header("X-Inventory-Service-Token",System.getenv("SUPPORT_REMOTE_TOKEN"))
                        .header("Content-Type","application/json");
                builder.method(exchange.getRequestMethod(),body.length==0?HttpRequest.BodyPublishers.noBody():HttpRequest.BodyPublishers.ofByteArray(body));
                var result=wire.send(builder.build(),HttpResponse.BodyHandlers.ofByteArray());
                boolean lose="POST".equals(exchange.getRequestMethod()) && loseCommand!=null
                        &&new String(body,java.nio.charset.StandardCharsets.UTF_8).contains("\"commandId\":\""+loseCommand+"\"");
                byte[] response=lose?"upstream reply lost after commit".getBytes():result.body();
                exchange.getResponseHeaders().set("Content-Type","application/json");
                exchange.sendResponseHeaders(lose?503:result.statusCode(),response.length);
                exchange.getResponseBody().write(response);
            } catch(Exception failure) { exchange.sendResponseHeaders(502,-1); }
            finally {exchange.close();}
        });
        proxy.setExecutor(workers);proxy.start();
        p.add("local-life.inventory.service-url",()->"http://127.0.0.1:"+proxy.getAddress().getPort());
        p.add("local-life.inventory.service-token",()->System.getenv("SUPPORT_REMOTE_TOKEN"));
        p.add("spring.datasource.url",()->System.getenv("SUPPORT_MYSQL_URL"));
        p.add("spring.datasource.username",()->System.getenv("SUPPORT_MYSQL_USER"));
        p.add("spring.datasource.password",()->System.getenv("SUPPORT_MYSQL_PASSWORD"));
        p.add("spring.datasource.driver-class-name",()->"com.mysql.cj.jdbc.Driver");
        p.add("spring.sql.init.mode",()->"never");p.add("spring.flyway.enabled",()->"true");
    }
    @AfterAll static void stop(){if(proxy!=null)proxy.stop(0);workers.shutdownNow();}
    @Autowired JdbcTemplate jdbc;
    @Autowired InventoryService inventory;
    @Autowired InventoryCommandDelivery delivery;
    @Autowired CartOrderService carts;
    @Autowired PaymentService payments;
    @Autowired OrderService orders;
    @Autowired SupportService support;
    @Autowired SupportOperations operations;
    @Autowired SupportReceiptSimulator simulator;
    @Autowired SupportStockEffects effects;
    @Autowired SupportInventorySimulatorController inventorySimulator;
    @MockitoBean CommerceCatalogPort catalog;
    JdbcTemplate remote;
    String user,order;long item;
    @BeforeEach void setup(){
        loseCommand=null;user=UUID.randomUUID().toString();item=900000000L+new java.security.SecureRandom().nextInt(100000000);
        remote=new JdbcTemplate(new DriverManagerDataSource(System.getenv("SUPPORT_INVENTORY_DB_URL"),System.getenv("SUPPORT_MYSQL_USER"),System.getenv("SUPPORT_MYSQL_PASSWORD")));
        jdbc.update("INSERT INTO user_account(id,username,password_hash,enabled,token_version) VALUES(?,?,'x',TRUE,0)",user,user);
        inventory.createStock("PRODUCT",item,20);
        when(catalog.requireItem(eq("PRODUCT"),eq(item))).thenReturn(new CommerceItemSnapshot("PRODUCT",item,"remote fixture",101,"CNY",1,
                "{\"saleSpecification\":{\"code\":\"black-256\",\"color\":\"black\",\"storage\":\"256GB\"}}"));
        order=carts.create(new CreateCartOrderRequest(List.of(new CreateCartOrderRequest.Line("PRODUCT",item,3)),null),user,"cart").id();
        for(String id:jdbc.query("SELECT command_id FROM inventory_command_journal WHERE order_id=? AND status='TRY'",(rs,n)->rs.getString(1),order)) delivery.reconcileTry(id,order);
        var payment=payments.create(order,user);payments.simulateSuccess(payment.id(),user);
        assertThat(delivery.deliver("confirm:"+order)).isTrue();
        jdbc.update("UPDATE fulfillment_task SET status='SHIPPED',fence=1 WHERE order_id=?",order);orders.complete(order,user);
    }
    SupportService.CaseView inspected(AfterSalePolicy.Type type){
        var card=support.preview(new SupportService.Request(order,item,1,type,"remote acceptance"),user);
        var c=support.confirm(card.previewId(),user,"apply");
        c=operations.submitReturn(c.id(),user,c.version(),"REMOTE-RETURN","return");
        c=operations.reconcile(simulator.record(c.id(),new SupportReceiptSimulator.Input(Event.RETURN_RECEIVED,c.version(),item,1,null,"warehouse"),"operator","received").id(),user);
        return operations.reconcile(simulator.record(c.id(),new SupportReceiptSimulator.Input(Event.INSPECTION_ACCEPTED,c.version(),item,1,true,"inspection"),"operator","inspected").id(),user);
    }
    void lostDelivery(String id){
        loseCommand=id;assertThat(delivery.deliver(id)).isFalse();
        assertThat(jdbc.queryForObject("SELECT status FROM inventory_command_journal WHERE command_id=?",String.class,id)).isEqualTo("PENDING");
        assertThat(remote.queryForObject("SELECT COUNT(*) FROM inventory_command_receipt WHERE command_id=?",Integer.class,id)).isEqualTo(1);
    }
    void recover(String id){loseCommand=null;assertThat(delivery.deliver(id)).isTrue();assertThat(delivery.deliver(id)).isTrue();}
    @Test void simulatorRetriesAssociatedOriginalCommandAndRejectsWrongCase() {
        String id=inspected(AfterSalePolicy.Type.RETURN_REFUND).id(),command="return:"+id;
        assertThat(effects.apply(command)).isFalse();
        var admin=new org.springframework.security.authentication.UsernamePasswordAuthenticationToken("operator","unused",
                List.of(new org.springframework.security.core.authority.SimpleGrantedAuthority("ROLE_ADMIN")));
        var request=new SupportInventorySimulatorController.Retry(command);
        assertThatThrownBy(()->inventorySimulator.retryCase(UUID.randomUUID().toString(),request,admin))
                .isInstanceOf(com.example.locallife.common.ResourceNotFoundException.class);
        loseCommand=command;
        assertThat(inventorySimulator.retryCase(id,request,admin).data().get("status")).isEqualTo("PENDING");
        assertThat(remote.queryForObject("SELECT COUNT(*) FROM inventory_command_receipt WHERE command_id=?",Integer.class,command)).isEqualTo(1);
        int available=remote.queryForObject("SELECT available_quantity FROM inventory_stock WHERE item_id=?",Integer.class,item);
        loseCommand=null;
        assertThat(inventorySimulator.retryCase(id,request,admin).data().get("status")).isEqualTo("ACK");
        assertThat(inventorySimulator.retryCase(id,request,admin).data().get("status")).isEqualTo("ACK");
        assertThat(remote.queryForObject("SELECT available_quantity FROM inventory_stock WHERE item_id=?",Integer.class,item)).isEqualTo(available);
        assertThat(effects.apply(command)).isTrue();
    }
    void held(String id){assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM support_order_claim WHERE case_id=?",Integer.class,id)).isEqualTo(1);}
    @Test void lostReturnReceiptBlocksRefundUntilOriginalInventoryAck(){
        var c=inspected(AfterSalePolicy.Type.RETURN_REFUND);String id=c.id(),effect="return:"+id;
        assertThat(effects.apply(effect)).isFalse();lostDelivery(effect);
        var refund=simulator.refundSucceeded(id,"operator","refund");
        assertThatThrownBy(()->operations.reconcile(refund.id(),user)).isInstanceOf(BusinessConflictException.class);
        held(id);assertThat(jdbc.queryForObject("SELECT refunded_minor FROM order_line_allocation WHERE order_id=?",Long.class,order)).isZero();
        recover(effect);assertThat(effects.apply(effect)).isTrue();
        assertThat(operations.reconcile(refund.id(),user).phase()).isEqualTo(Phase.COMPLETED);
        operations.reconcile(refund.id(),user);
        assertThat(jdbc.queryForObject("SELECT refunded_minor FROM order_line_allocation WHERE order_id=?",Long.class,order)).isEqualTo(101);
        assertThat(remote.queryForObject("SELECT available_quantity FROM inventory_stock WHERE item_id=?",Integer.class,item)).isEqualTo(18);
    }
    String readyExchange(){
        String id=inspected(AfterSalePolicy.Type.EXCHANGE).id(),effect="return:"+id;
        assertThat(effects.apply(effect)).isFalse();recover(effect);assertThat(effects.apply(effect)).isTrue();
        assertThat(operations.reserveReplacement(id,user).phase()).isEqualTo(Phase.WAITING_STOCK);
        String replacement=jdbc.queryForObject("SELECT id FROM support_replacement WHERE case_id=?",String.class,id);
        lostDelivery("replacement-reserve:"+replacement);
        assertThatThrownBy(()->operations.previewConversion(id,user)).isInstanceOf(BusinessConflictException.class);held(id);
        recover("replacement-reserve:"+replacement);
        assertThat(operations.reserveReplacement(id,user).phase()).isEqualTo(Phase.REPLACEMENT_READY);
        return id;
    }
    @Test void lostDispatchAckRetainsPendingReceiptAndProtectsReservation(){
        String id=readyExchange();String replacement=jdbc.queryForObject("SELECT id FROM support_replacement WHERE case_id=?",String.class,id);
        var dispatch=simulator.replacementEvent(id,false,"REMOTE-SHIP","operator","dispatch");
        assertThat(operations.reconcile(dispatch.id(),user).phase()).isEqualTo(Phase.REPLACEMENT_READY);
        lostDelivery("replacement-dispatch:"+replacement);
        jdbc.update("UPDATE support_replacement SET reserve_until=? WHERE id=?",Timestamp.from(Instant.now().minusSeconds(1)),replacement);
        assertThat(operations.expireReplacement(id,user).phase()).isEqualTo(Phase.REPLACEMENT_READY);held(id);
        assertThat(jdbc.queryForObject("SELECT status FROM support_receipt WHERE id=?",String.class,dispatch.id())).isEqualTo("PENDING");
        recover("replacement-dispatch:"+replacement);
        assertThat(operations.reconcile(dispatch.id(),user).phase()).isEqualTo(Phase.REPLACEMENT_SHIPPED);
        operations.reconcile(dispatch.id(),user);
        assertThat(remote.queryForObject("SELECT sold_quantity FROM inventory_stock WHERE item_id=?",Integer.class,item)).isEqualTo(3);
    }
    @Test void lostReleaseAckBlocksConversionUntilOriginalCommandIsConfirmed(){
        String id=readyExchange();String replacement=jdbc.queryForObject("SELECT id FROM support_replacement WHERE case_id=?",String.class,id);
        jdbc.update("UPDATE support_replacement SET reserve_until=? WHERE id=?",Timestamp.from(Instant.now().minusSeconds(1)),replacement);
        assertThat(operations.expireReplacement(id,user).phase()).isEqualTo(Phase.REPLACEMENT_RELEASING);
        lostDelivery("replacement-release:"+replacement);
        assertThatThrownBy(()->operations.previewConversion(id,user)).isInstanceOf(BusinessConflictException.class);held(id);
        recover("replacement-release:"+replacement);
        assertThat(operations.expireReplacement(id,user).phase()).isEqualTo(Phase.WAITING_CHOICE);
        assertThat(operations.previewConversion(id,user).amountMinor()).isEqualTo(101);held(id);
        assertThat(remote.queryForObject("SELECT available_quantity FROM inventory_stock WHERE item_id=?",Integer.class,item)).isEqualTo(18);
    }
}
