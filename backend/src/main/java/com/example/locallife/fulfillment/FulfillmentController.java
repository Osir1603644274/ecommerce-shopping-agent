package com.example.locallife.fulfillment;

import com.example.locallife.common.ApiResponse;
import com.example.locallife.common.ForbiddenOperationException;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;

@RestController
class FulfillmentController {
    private final FulfillmentLifecycle lifecycle;
    private final FulfillmentDeadLetters deadLetters;
    FulfillmentController(FulfillmentLifecycle lifecycle, FulfillmentDeadLetters deadLetters) {
        this.lifecycle = lifecycle; this.deadLetters = deadLetters;
    }

    @GetMapping("/api/orders/{orderId}/fulfillment")
    ApiResponse<FulfillmentLifecycle.View> get(@PathVariable String orderId, Authentication authentication) {
        return ApiResponse.ok(lifecycle.get(orderId, authentication.getName()));
    }

    @PostMapping("/api/admin/fulfillment/{orderId}/retry")
    ApiResponse<String> retry(@PathVariable String orderId, Authentication authentication) {
        if (authentication.getAuthorities().stream().noneMatch(a -> "ROLE_ADMIN".equals(a.getAuthority())))
            throw new ForbiddenOperationException("需要管理员权限");
        lifecycle.retry(orderId, authentication.getName());
        return ApiResponse.ok("已安排对账重试");
    }

    @PostMapping("/api/admin/fulfillment/events/{eventId}/replay")
    ApiResponse<String> replay(@PathVariable String eventId, Authentication authentication) {
        if (authentication.getAuthorities().stream().noneMatch(a -> "ROLE_ADMIN".equals(a.getAuthority())))
            throw new ForbiddenOperationException("需要管理员权限");
        deadLetters.replay(eventId, authentication.getName());
        return ApiResponse.ok("原始事件已重放，死信证据保留");
    }
}
