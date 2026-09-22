package com.example.locallife.support;

import com.example.locallife.common.*;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;

/** Admin-only transport. Customer Agent tools must never expose these endpoints. */
@RestController
@RequestMapping("/api/admin/support-simulator")
public class SupportSimulatorController {
    private final SupportReceiptSimulator simulator;private final SupportOperations operations;private final JdbcTemplate jdbc;
    private final SupportOrderSimulator orderSimulator;
    private final SupportScenarioClock clock;
    private final SupportService service;private final SupportStockEffects stockEffects;private final boolean enabled;
    public SupportSimulatorController(SupportReceiptSimulator simulator,SupportOperations operations,JdbcTemplate jdbc,SupportOrderSimulator orderSimulator,SupportScenarioClock clock,SupportService service,SupportStockEffects stockEffects,
            @org.springframework.beans.factory.annotation.Value("${local-life.support.simulator-enabled:false}") boolean enabled) {
        this.simulator=simulator;this.operations=operations;this.jdbc=jdbc;
        this.orderSimulator=orderSimulator;
        this.clock=clock;
        this.service=service;this.stockEffects=stockEffects;
        this.enabled=enabled;
    }
    private String owner(String id) {
        return jdbc.query("SELECT user_id FROM support_case WHERE id=?",(rs,n)->rs.getString(1),id).stream().findFirst()
                .orElseThrow(()->new ResourceNotFoundException("售后单不存在"));
    }
    @GetMapping("/cases/{id}")
    public ApiResponse<java.util.Map<String,Object>> caseState(@PathVariable String id,Authentication auth) {
        requireAdmin(auth);String owner=owner(id);
        return ApiResponse.ok(java.util.Map.of("case",service.get(id,owner),"events",service.events(id,owner),
                "receipts",jdbc.queryForList("SELECT id,event_type,status,expected_version,attempts,last_error FROM support_receipt WHERE case_id=? ORDER BY created_at,id",id),
                "stockEffects",jdbc.queryForList("SELECT effect_id,kind,status,attempts,last_error FROM support_stock_effect WHERE case_id=? ORDER BY created_at,effect_id",id),
                "recovery",jdbc.queryForMap("SELECT recovery_attempts,recovery_error,recovery_next_at FROM support_case WHERE id=?",id),
                "replacements",jdbc.queryForList("SELECT id,status,reserve_until,tracking_no FROM support_replacement WHERE case_id=? ORDER BY created_at,id",id)));
    }
    @PostMapping("/cases/{id}/process")
    public ApiResponse<SupportService.CaseView> process(@PathVariable String id,Authentication auth) {
        requireAdmin(auth);String owner=owner(id);
        for(String effect:jdbc.query("SELECT effect_id FROM support_stock_effect WHERE case_id=? AND status IN ('PENDING','NEEDS_REVIEW')",(rs,n)->rs.getString(1),id)) stockEffects.apply(effect);
        var current=service.get(id,owner);
        if(java.util.Set.of(AfterSaleLifecycle.Phase.REPLACEMENT_READY,AfterSaleLifecycle.Phase.REPLACEMENT_RELEASING).contains(current.phase()))
            return ApiResponse.ok(operations.expireReplacement(id,owner));
        if(current.type()==AfterSalePolicy.Type.EXCHANGE && current.phase()==AfterSaleLifecycle.Phase.WAITING_STOCK)
            return ApiResponse.ok(operations.reserveReplacement(id,owner));
        return ApiResponse.ok(current);
    }
    public record ReviewDecision(long expectedVersion,String ticketId) { }
    @PostMapping("/cases/{id}/resume-review")
    public ApiResponse<SupportService.CaseView> resumeReview(@PathVariable String id,@RequestBody ReviewDecision body,
            @RequestHeader("Idempotency-Key") String key,Authentication auth) {
        requireAdmin(auth);return ApiResponse.ok(operations.resumeReview(id,owner(id),body.expectedVersion(),body.ticketId(),auth.getName(),key));
    }
    @PostMapping("/cases/{id}/receipts")
    public ApiResponse<SupportReceiptSimulator.Receipt> record(@PathVariable String id,
            @RequestBody SupportReceiptSimulator.Input input,@RequestHeader("Idempotency-Key") String key,Authentication auth) {
        requireAdmin(auth);return ApiResponse.ok(simulator.record(id,input,auth.getName(),key));
    }
    @PostMapping("/cases/{id}/refund-success")
    public ApiResponse<SupportReceiptSimulator.Receipt> refund(@PathVariable String id,
            @RequestHeader("Idempotency-Key") String key,Authentication auth) {
        requireAdmin(auth);return ApiResponse.ok(simulator.refundSucceeded(id,auth.getName(),key));
    }
    @PostMapping("/receipts/{receiptId}/apply")
    public ApiResponse<SupportService.CaseView> apply(@PathVariable String receiptId,Authentication auth) {
        requireAdmin(auth);
        var receipt=simulator.get(receiptId);
        String owner=jdbc.queryForObject("SELECT user_id FROM support_case WHERE id=?",String.class,receipt.caseId());
        return ApiResponse.ok(operations.reconcile(receiptId,owner));
    }
    private void requireAdmin(Authentication auth) {
        if(auth==null || auth.getAuthorities().stream().noneMatch(a->"ROLE_ADMIN".equals(a.getAuthority())))
            throw new ForbiddenOperationException("需要模拟器管理员权限");
        if(!enabled) throw new ForbiddenOperationException("售后模拟器未启用");
    }
    public record Shipment(String trackingNo) { }
    public record ClockAdvance(long expectedVersion,long seconds) { }
    @PostMapping("/orders/{id}/clock")
    public ApiResponse<SupportScenarioClock.View> bindClock(@PathVariable String id,Authentication auth) {
        requireAdmin(auth);return ApiResponse.ok(clock.bind(id,auth.getName()));
    }
    @PostMapping("/orders/{id}/clock/advance")
    public ApiResponse<SupportScenarioClock.View> advanceClock(@PathVariable String id,@RequestBody ClockAdvance body,
            @RequestHeader("Idempotency-Key") String key,Authentication auth) {
        requireAdmin(auth);return ApiResponse.ok(clock.advance(id,body.expectedVersion(),body.seconds(),auth.getName(),key));
    }
    @PostMapping("/orders/{id}/dispatch")
    public ApiResponse<SupportOrderSimulator.Receipt> orderDispatch(@PathVariable String id,@RequestBody Shipment body,
            @RequestHeader("Idempotency-Key") String key,Authentication auth) {
        requireAdmin(auth);return ApiResponse.ok(orderSimulator.record(id,false,body.trackingNo(),auth.getName(),key));
    }
    @PostMapping("/orders/{id}/received")
    public ApiResponse<SupportOrderSimulator.Receipt> orderReceived(@PathVariable String id,@RequestBody Shipment body,
            @RequestHeader("Idempotency-Key") String key,Authentication auth) {
        requireAdmin(auth);return ApiResponse.ok(orderSimulator.record(id,true,body.trackingNo(),auth.getName(),key));
    }
    @PostMapping("/order-receipts/{id}/apply")
    public ApiResponse<SupportOrderSimulator.Receipt> orderApply(@PathVariable String id,Authentication auth) {
        requireAdmin(auth);return ApiResponse.ok(orderSimulator.apply(id));
    }
    @PostMapping("/cases/{id}/replacement-dispatch")
    public ApiResponse<SupportReceiptSimulator.Receipt> dispatch(@PathVariable String id,@RequestBody Shipment body,
            @RequestHeader("Idempotency-Key") String key,Authentication auth) {
        requireAdmin(auth);return ApiResponse.ok(simulator.replacementEvent(id,false,body.trackingNo(),auth.getName(),key));
    }
    @PostMapping("/cases/{id}/replacement-received")
    public ApiResponse<SupportReceiptSimulator.Receipt> received(@PathVariable String id,@RequestBody Shipment body,
            @RequestHeader("Idempotency-Key") String key,Authentication auth) {
        requireAdmin(auth);return ApiResponse.ok(simulator.replacementEvent(id,true,body.trackingNo(),auth.getName(),key));
    }
}
