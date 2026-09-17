package com.example.locallife.ordering;

import com.example.locallife.common.ApiResponse;
import jakarta.validation.Valid;
import org.springframework.http.*;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/orders/cart")
public class CartOrderController {
    private final CartOrderService service;
    public CartOrderController(CartOrderService service) { this.service = service; }
    @PostMapping
    public ResponseEntity<ApiResponse<OrderResponse>> create(@Valid @RequestBody CreateCartOrderRequest request,
            @RequestHeader(name="Idempotency-Key",required=false) String key, Authentication authentication) {
        return ResponseEntity.status(HttpStatus.CREATED).body(ApiResponse.ok(service.create(request, authentication.getName(), key)));
    }
}
