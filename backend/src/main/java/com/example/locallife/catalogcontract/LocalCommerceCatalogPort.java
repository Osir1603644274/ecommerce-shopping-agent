package com.example.locallife.catalogcontract;

import com.example.locallife.ordering.CommerceCatalogPort;
import com.example.locallife.ordering.CommerceItemSnapshot;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Component;

@Component
@Profile("!cloud-trade")
class LocalCommerceCatalogPort implements CommerceCatalogPort {
    private final CommerceCatalogReadService service;

    LocalCommerceCatalogPort(CommerceCatalogReadService service) {
        this.service = service;
    }

    @Override
    public CommerceItemSnapshot requireItem(String itemType, Long itemId) {
        return service.requireItem(itemType, itemId);
    }
}
