package com.example.locallife.ordering;

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
import org.springframework.web.bind.annotation.RequestParam;

import java.util.List;

@RestController
@RequestMapping("/api/orders")
public class OrderController {
    private final OrderService service;
    private final OrderPageService pages;

    public OrderController(OrderService service, OrderPageService pages) {
        this.service = service;
        this.pages = pages;
    }

    @PostMapping
    public ResponseEntity<ApiResponse<OrderResponse>> create(
            @Valid @RequestBody CreateOrderRequest request,
            @RequestHeader(name = "Idempotency-Key", required = false) String idempotencyKey,
            Authentication authentication
    ) {
        return ResponseEntity.status(HttpStatus.CREATED)
                .body(ApiResponse.ok(service.create(
                        request, authentication.getName(), idempotencyKey)));
    }

    @PostMapping("/preview")
    public ApiResponse<OrderPreviewResponse> preview(
            @Valid @RequestBody CreateOrderRequest request,
            Authentication authentication
    ) {
        return ApiResponse.ok(service.preview(request, authentication.getName()));
    }

    @GetMapping
    public ApiResponse<List<OrderResponse>> listMine(Authentication authentication) {
        return ApiResponse.ok(service.listMine(authentication.getName()));
    }

    @GetMapping("/page")
    public ApiResponse<OrderPage> page(Authentication authentication,
            @RequestParam(defaultValue = "20") int size,
            @RequestParam(required = false) String status,
            @RequestParam(required = false) String cursor) {
        return ApiResponse.ok(pages.page(authentication.getName(), size, status, cursor));
    }

    @GetMapping("/by-idempotency-key/{idempotencyKey}")
    public ApiResponse<OrderResponse> getByIdempotencyKey(
            @PathVariable String idempotencyKey,
            Authentication authentication
    ) {
        return ApiResponse.ok(service.getByIdempotencyKey(
                idempotencyKey, authentication.getName()));
    }

    @GetMapping("/{orderReference}")
    public ApiResponse<OrderResponse> get(
            @PathVariable String orderReference,
            Authentication authentication
    ) {
        return ApiResponse.ok(service.get(
                orderReference, authentication.getName(), isAdminOrService(authentication)));
    }

    @PostMapping("/{orderId}/cancel")
    public ApiResponse<OrderResponse> cancel(
            @PathVariable String orderId,
            Authentication authentication
    ) {
        return ApiResponse.ok(service.cancel(
                orderId, authentication.getName(), isAdminOrService(authentication)));
    }

    @PostMapping("/{orderId}/complete")
    public ApiResponse<OrderResponse> complete(
            @PathVariable String orderId,
            Authentication authentication
    ) {
        return ApiResponse.ok(service.complete(orderId, authentication.getName()));
    }

    private static boolean isAdminOrService(Authentication authentication) {
        return authentication.getAuthorities().stream()
                .anyMatch(authority -> "ROLE_ADMIN".equals(authority.getAuthority())
                        || "ROLE_SERVICE".equals(authority.getAuthority()));
    }
}
