package com.example.locallife.search;

import com.example.locallife.integration.DomainEventTypes;
import com.example.locallife.integration.EventEnvelope;
import com.example.locallife.integration.ProductSearchEventPayload;
import com.example.locallife.product.Product;
import com.example.locallife.product.ProductRepository;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import org.junit.jupiter.api.Test;

import java.time.LocalDateTime;
import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.*;

class ProductChangedProjectionTests {
    private final ProductRepository products = mock(ProductRepository.class);
    private final ProductSearchProjectionCursorMapper cursor = mock(ProductSearchProjectionCursorMapper.class);
    private final ElasticsearchGateway gateway = mock(ElasticsearchGateway.class);
    private final ObjectMapper json = new ObjectMapper();
    private final ProductChangedProjection projection = new ProductChangedProjection(
            products, cursor, Optional.of(gateway), json, new SimpleMeterRegistry(), mock(CacheInvalidationPublisher.class));

    @Test
    void appliesNewerUpsertAndAdvancesCursorAfterIndexing() throws Exception {
        Product product = product("ACTIVE", 7L);
        when(cursor.findVersionForUpdate(1001L)).thenReturn(6L);
        when(products.findById(1001L)).thenReturn(Optional.of(product));
        EventEnvelope event = event("event-7", product, "UPSERT");

        projection.handle(event);

        verify(gateway).indexProduct(product, 7L);
        verify(cursor).advance(1001L, 7L, "event-7", "UPSERT");
    }

    @Test
    void rejectsDuplicateOrOutOfOrderWithoutTouchingElasticsearch() throws Exception {
        Product product = product("ACTIVE", 7L);
        when(cursor.findVersionForUpdate(1001L)).thenReturn(8L);

        projection.handle(event("event-7", product, "UPSERT"));

        verifyNoInteractions(gateway);
        verify(products, never()).findById(anyLong());
    }

    @Test
    void failsClosedWhenPayloadVersionDoesNotMatchMysql() throws Exception {
        Product product = product("ACTIVE", 8L);
        when(products.findById(1001L)).thenReturn(Optional.of(product));
        when(cursor.findVersionForUpdate(1001L)).thenReturn(6L);

        assertThatThrownBy(() -> projection.handle(event("event-7", product("ACTIVE", 7L), "UPSERT")))
                .isInstanceOf(IllegalStateException.class)
                .hasMessageContaining("does not match MySQL");
        verifyNoInteractions(gateway);
    }

    private EventEnvelope event(String id, Product product, String operation) throws Exception {
        String key = "product:" + product.id() + ":v" + product.entityVersion() + ":" + operation;
        String type = "DELETE".equals(operation)
                ? DomainEventTypes.PRODUCT_SEARCH_DELETE_V1
                : DomainEventTypes.PRODUCT_SEARCH_UPSERT_V1;
        return new EventEnvelope(
                id, "PRODUCT", product.id().toString(), type,
                json.writeValueAsString(new ProductSearchEventPayload(
                        product.id(), product.entityVersion(), operation, key)),
                LocalDateTime.now().minusSeconds(1)
        );
    }

    private static Product product(String status, Long version) {
        return new Product(
                1001L, "source", "item-1", "测试手机", "品牌", "卖家",
                "手机", "手机通讯", "智能手机", 299900L, "CNY", "verified",
                status, version, "12GB", "fixture", "v1", "MIT", "https://example.test",
                LocalDateTime.of(2026, 8, 29, 0, 0)
        );
    }
}
