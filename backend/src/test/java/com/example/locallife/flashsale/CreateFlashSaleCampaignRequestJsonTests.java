package com.example.locallife.flashsale;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import org.junit.jupiter.api.Test;

import java.time.Instant;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class CreateFlashSaleCampaignRequestJsonTests {
    private final ObjectMapper objectMapper = new ObjectMapper()
            .registerModule(new JavaTimeModule());

    @Test
    void acceptsExplicitOffsetAndPreservesTheRepresentedInstant() throws Exception {
        CreateFlashSaleCampaignRequest request = objectMapper.readValue("""
                {
                  "itemType": "PRODUCT",
                  "itemId": 1001,
                  "title": "手机秒杀",
                  "salePriceMinor": 199900,
                  "totalStock": 2,
                  "startsAt": "2026-09-01T15:00:00+08:00",
                  "endsAt": "2026-09-01T15:30:00+08:00"
                }
                """, CreateFlashSaleCampaignRequest.class);

        assertThat(request.startsAt().toInstant())
                .isEqualTo(Instant.parse("2026-09-01T07:00:00Z"));
        assertThat(request.endsAt().toInstant())
                .isEqualTo(Instant.parse("2026-09-01T07:30:00Z"));
    }

    @Test
    void rejectsAmbiguousLocalDateTimeWithoutOffset() {
        assertThatThrownBy(() -> objectMapper.readValue("""
                {
                  "itemType": "PRODUCT",
                  "itemId": 1001,
                  "title": "手机秒杀",
                  "salePriceMinor": 199900,
                  "totalStock": 2,
                  "startsAt": "2026-09-01T15:00:00",
                  "endsAt": "2026-09-01T15:30:00"
                }
                """, CreateFlashSaleCampaignRequest.class))
                .hasMessageContaining("OffsetDateTime");
    }
}
