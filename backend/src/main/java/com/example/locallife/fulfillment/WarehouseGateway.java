package com.example.locallife.fulfillment;

import java.util.Optional;

public interface WarehouseGateway {
    Optional<WarehouseReceipt> lookup(String requestKey);
    WarehouseReceipt dispatch(FulfillmentTask task);
}
