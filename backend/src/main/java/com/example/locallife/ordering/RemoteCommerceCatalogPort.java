package com.example.locallife.ordering;

import com.example.locallife.common.ApiResponse;
import com.example.locallife.common.ResourceNotFoundException;
import feign.FeignException;
import io.github.resilience4j.bulkhead.annotation.Bulkhead;
import io.github.resilience4j.circuitbreaker.annotation.CircuitBreaker;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Component;

@Component
@Profile("cloud-trade")
class RemoteCommerceCatalogPort implements CommerceCatalogPort {
    private final RemoteCommerceCatalogClient client;
    private final String internalToken;

    RemoteCommerceCatalogPort(
            RemoteCommerceCatalogClient client,
            @Value("${local-life.deployment.internal-token:}") String internalToken
    ) {
        this.client = client;
        this.internalToken = internalToken;
    }

    @Override
    @Bulkhead(name = "catalogRead", type = Bulkhead.Type.SEMAPHORE)
    @CircuitBreaker(name = "catalogRead")
    public CommerceItemSnapshot requireItem(String itemType, Long itemId) {
        try {
            ApiResponse<CommerceItemSnapshot> response =
                    client.getItem(itemType, itemId, internalToken);
            if (response == null || !response.success() || response.data() == null) {
                throw new CatalogUnavailableException("商品服务返回了无效合同");
            }
            if(!itemId.equals(response.data().itemId()) || !itemType.equals(response.data().itemType())
                || response.data().unitPriceMinor()<0 || response.data().currency()==null)
                throw new CatalogUnavailableException("商品服务合同身份不匹配");
            return response.data();
        } catch (FeignException.NotFound exception) {
            throw new ResourceNotFoundException("交易商品不存在");
        } catch (FeignException exception) {
            throw new CatalogUnavailableException("商品服务暂时不可用", exception);
        }
    }
}
