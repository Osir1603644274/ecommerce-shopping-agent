package com.example.locallife.payment;

import com.example.locallife.common.ApiResponse;
import jakarta.validation.Valid;
import org.springframework.http.*;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;
import java.util.List;

@RestController
@RequestMapping("/api/payments")
public class PartialRefundController {
    private final PartialRefundService service;
    private final LocalPartialRefundSimulator simulator;
    public PartialRefundController(PartialRefundService service,LocalPartialRefundSimulator simulator) {
        this.service=service;this.simulator=simulator;
    }
    @PostMapping("/orders/{orderId}/partial-refunds")
    public ResponseEntity<ApiResponse<PartialRefundView>> create(@PathVariable String orderId,
            @Valid @RequestBody PartialRefundRequest request,@RequestHeader(name="Idempotency-Key",required=false) String key,
            Authentication authentication) {
        return ResponseEntity.status(HttpStatus.CREATED).body(ApiResponse.ok(service.create(orderId,authentication.getName(),key,request)));
    }
    @GetMapping("/orders/{orderId}/refund-balance")
    public ApiResponse<List<PartialRefundService.Balance>> balance(@PathVariable String orderId,Authentication authentication) {
        return ApiResponse.ok(service.balance(orderId,authentication.getName()));
    }
    @PostMapping("/orders/{orderId}/partial-refunds/preview")
    public ApiResponse<PartialRefundService.Quote> preview(@PathVariable String orderId,
            @Valid @RequestBody PartialRefundRequest request,Authentication authentication) {
        return ApiResponse.ok(service.quote(orderId,authentication.getName(),request));
    }
    @GetMapping("/orders/{orderId}/partial-refunds/by-key/{key}")
    public ApiResponse<PartialRefundView> byKey(@PathVariable String orderId,@PathVariable String key,Authentication authentication) {
        return ApiResponse.ok(service.byKey(orderId,authentication.getName(),key));
    }
    @GetMapping("/partial-refunds/{id}")
    public ApiResponse<PartialRefundView> get(@PathVariable String id,Authentication authentication) {
        return ApiResponse.ok(service.get(id,authentication.getName()));
    }
    @PostMapping("/partial-refunds/{id}/simulate-success")
    public ApiResponse<PartialRefundView> simulate(@PathVariable String id,Authentication authentication) {
        // Distinct beans and transactions: a dropped reply leaves a durable receipt for reconciliation.
        simulator.succeed(id,authentication.getName());
        return ApiResponse.ok(service.reconcile(id,authentication.getName()));
    }
    @PostMapping("/partial-refunds/{id}/reconcile")
    public ApiResponse<PartialRefundView> reconcile(@PathVariable String id,Authentication authentication) {
        return ApiResponse.ok(service.reconcile(id,authentication.getName()));
    }
}
