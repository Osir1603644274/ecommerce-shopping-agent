package com.example.locallife.flashsale;

import com.example.locallife.LocalLifeApplication;
import com.example.locallife.product.*;
import com.example.locallife.search.CacheInvalidationPublisher;
import org.springframework.boot.SpringApplication;
import org.springframework.context.annotation.*;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.core.annotation.Order;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.web.bind.annotation.*;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;

/** Lab-only HTTP controls, compiled only to test-classes; never packaged in production. */
@Configuration
@Profile("messaging-lab")
@Import(MessagingLabApplication.Controls.class)
public class MessagingLabApplication {
    public static void main(String[] args) { SpringApplication.run(LocalLifeApplication.class,args); }
    @Bean @Order(0) SecurityFilterChain labSecurity(HttpSecurity http) throws Exception {
        return http.securityMatcher("/lab/**").csrf(csrf->csrf.disable())
                .authorizeHttpRequests(auth->auth.anyRequest().permitAll()).build();
    }
    @Bean FlashSaleFaultProbe labFaultProbe(JdbcTemplate jdbc) {
        return (point,id)->{
            String mode=Controls.faults.get(point);
            jdbc.update("INSERT INTO lab_fault(point,request_id) VALUES(?,?)",point,id);
            if(mode==null) return;
            if("crash".equals(mode)) Runtime.getRuntime().halt(86);
            if("fail".equals(mode)) throw new IllegalStateException("Injected business failure");
        };
    }
    @RestController @RequestMapping("/lab") @Profile("messaging-lab")
    static class Controls {
        static final Map<String,String> faults=new ConcurrentHashMap<>();
        @Autowired JdbcTemplate jdbc;
        @Autowired FlashSaleService flash;
        @Autowired FlashSaleRedisGateway redis;
        @Autowired FlashSaleMapper campaigns;
        @Autowired ObjectProvider<FlashSaleRocketMq> mq;
        @Autowired FlashSaleRequestStore requests;
        @Autowired ProductService products;
        @Autowired ProductAdminService admin;
        @Autowired CacheInvalidationPublisher invalidation;
        @Autowired PlatformTransactionManager transactions;
        @Autowired org.springframework.context.ApplicationContext context;

        @GetMapping("/ready") Object ready() { return Map.of("ready",true,"pid",ProcessHandle.current().pid()); }
        @PostMapping("/fault") Object fault(@RequestBody Map<String,String> body) {
            if("clear".equals(body.get("point"))) faults.clear();
            else faults.put(body.get("point"),body.get("mode"));
            return faults;
        }
        @PostMapping("/seed") Object seed() {
            jdbc.execute("CREATE TABLE IF NOT EXISTS lab_fault(id BIGINT AUTO_INCREMENT PRIMARY KEY,point VARCHAR(64),request_id VARCHAR(36),created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)");
            String user=UUID.randomUUID().toString();
            jdbc.update("INSERT INTO user_account(id,username,password_hash,enabled,token_version) VALUES(?,?,'unused',true,0)",user,"lab-"+user);
            var c=flash.create(new CreateFlashSaleCampaignRequest("PRODUCT",1001L,"lab campaign",1234L,3,
                    java.time.OffsetDateTime.now().minusMinutes(1),java.time.OffsetDateTime.now().plusHours(1)));
            flash.activate(c.id());
            return Map.of("campaign",c.id(),"user",user);
        }
        @PostMapping("/purchase") Object purchase(@RequestBody Map<String,Object> body) {
            return flash.purchase(((Number)body.get("campaign")).longValue(),body.get("user").toString());
        }
        @PostMapping("/duplicate/{id}") Object duplicate(@PathVariable String id) throws Exception {
            var row=jdbc.queryForMap("SELECT * FROM flash_sale_request WHERE id=?",id);
            mq.getObject().send(new FlashSaleRocketMq.Command(id,((Number)row.get("campaign_id")).longValue(),row.get("user_id").toString(),((Number)row.get("amount_minor")).longValue()));
            return Map.of("sent",id);
        }
        @GetMapping("/state/{campaign}") Object state(@PathVariable long campaign) {
            return Map.of("campaign",jdbc.queryForMap("SELECT * FROM flash_sale_campaign WHERE id=?",campaign),
                    "requests",jdbc.queryForList("SELECT * FROM flash_sale_request WHERE campaign_id=?",campaign),
                    "orders",jdbc.queryForList("SELECT * FROM flash_sale_order WHERE campaign_id=?",campaign),
                    "faults",jdbc.queryForList("SELECT * FROM lab_fault ORDER BY id"),
                    "deadLetters",jdbc.queryForList("SELECT * FROM dead_letter_event WHERE source='FLASH_SALE'"));
        }
        @PostMapping("/product") Object createProduct(@RequestBody CreateProductRequest request) { return admin.create(request); }
        @GetMapping("/product/{id}") Object product(@PathVariable long id) { return products.get(id).orElseThrow(); }
        @PostMapping("/product/{id}") Object update(@PathVariable long id,@RequestParam(defaultValue="false") boolean rollback,@RequestBody ProductUpdateRequest request) {
            return new TransactionTemplate(transactions).execute(status->{
                var receipt=admin.update(id,request);
                if(rollback) status.setRollbackOnly();
                return Map.of("receipt",receipt,"rollback",rollback);
            });
        }
        @PostMapping("/notify/{id}") Object notifyProduct(@PathVariable long id) { invalidation.productUpdated(id); return Map.of("queued",id); }
        @GetMapping("/cache-outbox") Object cacheOutbox() { return jdbc.queryForList("SELECT * FROM cache_invalidation_outbox ORDER BY created_at,id"); }
        @PostMapping("/rabbit/{action}") Object rabbit(@PathVariable String action) {
            var container=context.getBean("cacheInvalidationListenerContainer",org.springframework.amqp.rabbit.listener.SimpleMessageListenerContainer.class);
            if("stop".equals(action)) container.stop(); else container.start();
            return Map.of("running",container.isRunning());
        }
        @PostMapping("/recover/{id}") Object recover(@PathVariable String id) {
            // Explicit SQL scan trigger only for requests whose original broker delivery is held.
            jdbc.update("UPDATE flash_sale_request SET created_at=DATE_SUB(CURRENT_TIMESTAMP,INTERVAL 10 MINUTE),next_attempt_at=CURRENT_TIMESTAMP WHERE id=?",id);
            context.getBean(FlashSaleRequestRecovery.class).recover();
            return Map.of("recovered",id);
        }
    }
}
