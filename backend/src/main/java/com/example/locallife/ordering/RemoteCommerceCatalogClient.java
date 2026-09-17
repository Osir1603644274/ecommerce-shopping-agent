package com.example.locallife.ordering;

import com.example.locallife.common.ApiResponse;
import org.springframework.cloud.openfeign.FeignClient;
import org.springframework.context.annotation.Profile;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestHeader;

@Profile("cloud-trade")
@FeignClient(
        name = "commerce-catalog",
        contextId = "commerceCatalog",
        url = "${local-life.trade.catalog-base-url:http://catalog-search-service:8081}"
)
interface RemoteCommerceCatalogClient {
    @GetMapping("/internal/trade-catalog/items/{itemType}/{itemId}")
    ApiResponse<CommerceItemSnapshot> getItem(
            @PathVariable("itemType") String itemType,
            @PathVariable("itemId") Long itemId,
            @RequestHeader("X-Internal-Service-Token") String internalToken
    );
}
