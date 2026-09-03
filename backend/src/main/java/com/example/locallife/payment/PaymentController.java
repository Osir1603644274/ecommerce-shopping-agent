package com.example.locallife.payment;

import com.example.locallife.common.ApiResponse;
import jakarta.validation.Valid;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/payments")
public class PaymentController {
    private final PaymentService service;

    public PaymentController(PaymentService service) {
        this.service = service;
    }

    @PostMapping("/orders/{orderId}")
    public ResponseEntity<ApiResponse<PaymentRecord>> create(
            @PathVariable String orderId,
            Authentication authentication
    ) {
        return ResponseEntity.status(HttpStatus.CREATED)
                .body(ApiResponse.ok(service.create(orderId, authentication.getName())));
    }

    @GetMapping("/orders/{orderId}")
    public ApiResponse<PaymentRecord> getByOrder(
            @PathVariable String orderId,
            Authentication authentication
    ) {
        return ApiResponse.ok(service.getByOrderId(orderId, authentication.getName()));
    }

    @PostMapping("/callbacks/{provider}")
    public ApiResponse<PaymentRecord> callback(
            @PathVariable String provider,
            @Valid @RequestBody PaymentCallbackRequest request,
            @RequestHeader(name = "X-Payment-Signature", required = false) String signature
    ) {
        return ApiResponse.ok(service.processCallback(provider, request, signature));
    }

    @PostMapping("/{paymentId}/simulate-success")
    public ApiResponse<PaymentRecord> simulateSuccess(
            @PathVariable String paymentId,
            Authentication authentication
    ) {
        return ApiResponse.ok(service.simulateSuccess(paymentId, authentication.getName()));
    }

    @PostMapping("/orders/{orderId}/refunds")
    public ResponseEntity<ApiResponse<RefundRecord>> requestRefund(
            @PathVariable String orderId,
            @Valid @RequestBody RefundRequest request,
            Authentication authentication
    ) {
        return ResponseEntity.status(HttpStatus.CREATED)
                .body(ApiResponse.ok(service.requestRefund(
                        orderId, authentication.getName(), request.reason())));
    }

    @PostMapping("/refunds/{refundId}/simulate-success")
    public ApiResponse<RefundRecord> simulateRefundSuccess(
            @PathVariable String refundId,
            Authentication authentication
    ) {
        return ApiResponse.ok(service.simulateRefundSuccess(refundId, authentication.getName()));
    }
}
