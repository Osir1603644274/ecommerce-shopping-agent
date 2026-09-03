package com.example.locallife.integration;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.springframework.dao.DuplicateKeyException;

import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;
import org.mockito.ArgumentCaptor;

class OutboxServiceIdempotencyTests {
    @Test
    void deterministicKeyReusesByteEquivalentEventAndRejectsConflict() {
        OutboxMapper mapper = mock(OutboxMapper.class);
        OutboxService service = new OutboxService(
                mapper,
                new ObjectMapper(),
                Clock.fixed(Instant.parse("2026-08-29T00:00:00Z"), ZoneOffset.UTC)
        );
        when(mapper.insert(any())).thenReturn(1).thenThrow(new DuplicateKeyException("duplicate"));

        String first = service.appendIdempotent(
                "product:1:v2:UPSERT", "PRODUCT", "1", "product.search.upsert.v1",
                Map.of("productId", 1, "entityVersion", 2)
        );
        ArgumentCaptor<OutboxEvent> inserted = ArgumentCaptor.forClass(OutboxEvent.class);
        verify(mapper).insert(inserted.capture());
        OutboxEvent stored = new OutboxEvent(
                first, "PRODUCT", "1", "product.search.upsert.v1",
                inserted.getValue().payloadJson(), "PENDING", 0,
                java.time.LocalDateTime.of(2026, 8, 29, 0, 0),
                null, null, null, null,
                java.time.LocalDateTime.of(2026, 8, 29, 0, 0)
        );
        when(mapper.findById(first)).thenReturn(stored);

        assertThat(service.appendIdempotent(
                "product:1:v2:UPSERT", "PRODUCT", "1", "product.search.upsert.v1",
                Map.of("productId", 1, "entityVersion", 2)
        )).isEqualTo(first);

        doThrow(new DuplicateKeyException("duplicate")).when(mapper).insert(any());
        assertThatThrownBy(() -> service.appendIdempotent(
                "product:1:v2:UPSERT", "PRODUCT", "1", "product.search.upsert.v1",
                Map.of("productId", 1, "entityVersion", 3)
        )).isInstanceOf(IllegalStateException.class).hasMessageContaining("conflict");
    }
}
