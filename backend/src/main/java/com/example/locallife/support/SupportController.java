package com.example.locallife.support;

import com.example.locallife.common.ApiResponse;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;
import java.util.List;

@RestController
@RequestMapping("/api/after-sales")
public class SupportController {
    private final SupportService service;
    private final SupportOperations operations;
    public SupportController(SupportService service,SupportOperations operations) { this.service=service;this.operations=operations; }
    public record Confirmation(String previewId) { }
    public record Cancellation(long expectedVersion) { }
    public record ReturnShipment(long expectedVersion,String trackingNo) { }
    public record Reconcile(String receiptId) { }

    @PostMapping("/preview")
    public ApiResponse<SupportService.Preview> preview(@RequestBody SupportService.Request body,Authentication auth) {
        return ApiResponse.ok(service.preview(body,auth.getName()));
    }
    @PostMapping("/confirm")
    public ApiResponse<SupportService.CaseView> confirm(@RequestBody Confirmation body,
            @RequestHeader("Idempotency-Key") String key,Authentication auth) {
        return ApiResponse.ok(service.confirm(body.previewId(),auth.getName(),key));
    }
    @GetMapping("/{id}")
    public ApiResponse<SupportService.CaseView> get(@PathVariable String id,Authentication auth) {
        return ApiResponse.ok(service.get(id,auth.getName()));
    }
    @GetMapping("/orders/{orderId}")
    public ApiResponse<List<SupportService.CaseView>> list(@PathVariable String orderId,Authentication auth) {
        return ApiResponse.ok(service.list(orderId,auth.getName()));
    }
    @GetMapping("/{id}/events")
    public ApiResponse<List<SupportService.EventView>> events(@PathVariable String id,Authentication auth) {
        return ApiResponse.ok(service.events(id,auth.getName()));
    }
    @GetMapping("/{id}/receipts")
    public ApiResponse<List<SupportService.ReceiptView>> receipts(@PathVariable String id,Authentication auth) {
        return ApiResponse.ok(service.receipts(id,auth.getName()));
    }
    @PostMapping("/{id}/cancel")
    public ApiResponse<SupportService.CaseView> cancel(@PathVariable String id,@RequestBody Cancellation body,
            @RequestHeader("Idempotency-Key") String key,Authentication auth) {
        return ApiResponse.ok(service.cancel(id,auth.getName(),body.expectedVersion(),key));
    }
    @PostMapping("/{id}/return-shipment")
    public ApiResponse<SupportService.CaseView> shipment(@PathVariable String id,@RequestBody ReturnShipment body,
            @RequestHeader("Idempotency-Key") String key,Authentication auth) {
        return ApiResponse.ok(operations.submitReturn(id,auth.getName(),body.expectedVersion(),body.trackingNo(),key));
    }
    @PostMapping("/reconcile")
    public ApiResponse<SupportService.CaseView> reconcile(@RequestBody Reconcile body,Authentication auth) {
        return ApiResponse.ok(operations.reconcile(body.receiptId(),auth.getName()));
    }
    @PostMapping("/{id}/wait-stock")
    public ApiResponse<SupportService.CaseView> waitStock(@PathVariable String id,@RequestBody Cancellation body,
            @RequestHeader("Idempotency-Key") String key,Authentication auth) {
        return ApiResponse.ok(operations.chooseWait(id,auth.getName(),body.expectedVersion(),key));
    }
    @PostMapping("/{id}/conversion-preview")
    public ApiResponse<SupportOperations.ConversionPreview> conversionPreview(@PathVariable String id,Authentication auth) {
        return ApiResponse.ok(operations.previewConversion(id,auth.getName()));
    }
    @PostMapping("/conversion-confirm")
    public ApiResponse<SupportService.CaseView> conversionConfirm(@RequestBody Confirmation body,
            @RequestHeader("Idempotency-Key") String key,Authentication auth) {
        return ApiResponse.ok(operations.confirmConversion(body.previewId(),auth.getName(),key));
    }
}
