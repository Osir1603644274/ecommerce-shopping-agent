package com.example.locallife.marketing;

import com.example.locallife.common.ApiResponse;
import jakarta.validation.Valid;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

@RestController
@RequestMapping("/api/coupons")
public class CouponController {
    private final CouponService service;

    public CouponController(CouponService service) {
        this.service = service;
    }

    @PostMapping("/templates")
    @PreAuthorize("hasRole('ADMIN')")
    public ResponseEntity<ApiResponse<CouponTemplate>> createTemplate(
            @Valid @RequestBody CreateCouponTemplateRequest request
    ) {
        return ResponseEntity.status(HttpStatus.CREATED)
                .body(ApiResponse.ok(service.createTemplate(request)));
    }

    @PostMapping("/templates/{templateId}/claims")
    public ResponseEntity<ApiResponse<UserCoupon>> claim(
            @PathVariable Long templateId,
            Authentication authentication
    ) {
        return ResponseEntity.status(HttpStatus.CREATED)
                .body(ApiResponse.ok(service.claim(templateId, authentication.getName())));
    }

    @GetMapping("/mine")
    public ApiResponse<List<UserCoupon>> listMine(Authentication authentication) {
        return ApiResponse.ok(service.listMine(authentication.getName()));
    }
}
