package com.example.locallife.flashsale;

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
@RequestMapping("/api/flash-sales")
public class FlashSaleController {
    private final FlashSaleService service;
    private final FlashSaleConcurrencyMetrics concurrencyMetrics;

    public FlashSaleController(
            FlashSaleService service,
            FlashSaleConcurrencyMetrics concurrencyMetrics
    ) {
        this.service = service;
        this.concurrencyMetrics = concurrencyMetrics;
    }

    @GetMapping
    public ApiResponse<List<FlashSaleCampaign>> listCampaigns() {
        return ApiResponse.ok(service.listCampaigns());
    }

    @PostMapping
    @PreAuthorize("hasRole('ADMIN')")
    public ResponseEntity<ApiResponse<FlashSaleCampaign>> create(
            @Valid @RequestBody CreateFlashSaleCampaignRequest request
    ) {
        return ResponseEntity.status(HttpStatus.CREATED)
                .body(ApiResponse.ok(service.create(request)));
    }

    @PostMapping("/{campaignId}/activate")
    @PreAuthorize("hasRole('ADMIN')")
    public ApiResponse<FlashSaleCampaign> activate(@PathVariable Long campaignId) {
        return ApiResponse.ok(service.activate(campaignId));
    }

    @PostMapping("/{campaignId}/purchase")
    public ResponseEntity<ApiResponse<FlashSalePurchaseResponse>> purchase(
            @PathVariable Long campaignId,
            Authentication authentication
    ) {
        try (var ignored = concurrencyMetrics.enterHttpRequest()) {
            return ResponseEntity.status(HttpStatus.ACCEPTED)
                    .body(ApiResponse.ok(service.purchase(
                            campaignId, authentication.getName())));
        }
    }

    @GetMapping("/orders")
    public ApiResponse<List<FlashSaleOrder>> listMine(Authentication authentication) {
        return ApiResponse.ok(service.listMine(authentication.getName()));
    }
}
