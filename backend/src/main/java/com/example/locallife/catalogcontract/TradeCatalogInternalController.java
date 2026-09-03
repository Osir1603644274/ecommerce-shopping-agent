package com.example.locallife.catalogcontract;

import com.example.locallife.common.ApiResponse;
import com.example.locallife.ordering.CommerceItemSnapshot;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/internal/trade-catalog")
public class TradeCatalogInternalController {
    private final CommerceCatalogReadService service;

    public TradeCatalogInternalController(CommerceCatalogReadService service) {
        this.service = service;
    }

    @GetMapping("/items/{itemType}/{itemId}")
    public ApiResponse<CommerceItemSnapshot> getItem(
            @PathVariable String itemType,
            @PathVariable Long itemId
    ) {
        return ApiResponse.ok(service.requireItem(itemType, itemId));
    }
}
