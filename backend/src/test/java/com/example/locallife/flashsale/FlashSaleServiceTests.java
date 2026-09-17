package com.example.locallife.flashsale;

import com.example.locallife.common.BusinessConflictException;
import com.example.locallife.common.InvalidBusinessStateException;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.LocalDateTime;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

class FlashSaleServiceTests {
    private FlashSaleMapper mapper;
    private FlashSaleRedisGateway gateway;
    private FlashSaleService service;
    private FlashSaleCampaign campaign;

    @BeforeEach
    void setUp() {
        mapper = mock(FlashSaleMapper.class);
        gateway = mock(FlashSaleRedisGateway.class);
        Clock clock = Clock.fixed(Instant.parse("2026-07-30T04:00:00Z"), ZoneOffset.UTC);
        FlashSaleProperties properties = new FlashSaleProperties(
                true, "stream.flash", "group", Duration.ofMinutes(1), 8);
        FlashSaleRequestStore requests = mock(FlashSaleRequestStore.class);
        when(requests.accept(anyString(),anyLong(),anyString(),anyLong()))
                .thenAnswer(call -> call.getArgument(0));
        service = new FlashSaleService(mapper, gateway, properties, requests, clock);
        campaign = new FlashSaleCampaign(
                1L, "PRODUCT", 1001L, "手机秒杀", 199900L,
                2, 2,
                LocalDateTime.ofInstant(clock.instant().minusSeconds(60), ZoneOffset.UTC),
                LocalDateTime.ofInstant(clock.instant().plusSeconds(60), ZoneOffset.UTC),
                "ACTIVE", 0L, null, null
        );
        when(mapper.findCampaign(1L)).thenReturn(campaign);
    }

    @Test
    void acceptedPurchaseReturnsQueuedOrder() {
        when(gateway.purchase(anyLong(), anyString(), anyString(), anyLong()))
                .thenReturn(FlashSaleRedisGateway.ACCEPTED);

        FlashSalePurchaseResponse response = service.purchase(1L, "user-1");

        assertThat(response.orderId()).isNotBlank();
        assertThat(response.status()).isEqualTo("QUEUED");
    }

    @Test
    void duplicateAndSoldOutResultsBecomeBusinessConflicts() {
        when(gateway.purchase(anyLong(), anyString(), anyString(), anyLong()))
                .thenReturn(FlashSaleRedisGateway.DUPLICATE);
        assertThatThrownBy(() -> service.purchase(1L, "user-1"))
                .isInstanceOf(BusinessConflictException.class)
                .hasMessageContaining("只能购买一次");

        when(gateway.purchase(anyLong(), anyString(), anyString(), anyLong()))
                .thenReturn(FlashSaleRedisGateway.SOLD_OUT);
        assertThatThrownBy(() -> service.purchase(1L, "user-2"))
                .isInstanceOf(BusinessConflictException.class)
                .hasMessageContaining("售罄");
    }

    @Test
    void campaignRequestNormalizesExplicitOffsetToUtcBeforePersistence() {
        CreateFlashSaleCampaignRequest request = new CreateFlashSaleCampaignRequest(
                "PRODUCT", 1001L, "手机秒杀", 199900L, 2,
                OffsetDateTime.parse("2026-09-01T15:00:00+08:00"),
                OffsetDateTime.parse("2026-09-01T15:30:00+08:00")
        );

        FlashSaleMapper.MutableCampaign mutable =
                new FlashSaleMapper.MutableCampaign(request);

        assertThat(mutable.getStartsAt())
                .isEqualTo(LocalDateTime.parse("2026-09-01T07:00:00"));
        assertThat(mutable.getEndsAt())
                .isEqualTo(LocalDateTime.parse("2026-09-01T07:30:00"));
    }

    @Test
    void campaignWindowMustAdvanceByInstantRatherThanLocalClockText() {
        CreateFlashSaleCampaignRequest request = new CreateFlashSaleCampaignRequest(
                "PRODUCT", 1001L, "手机秒杀", 199900L, 2,
                OffsetDateTime.parse("2026-09-01T15:00:00+08:00"),
                OffsetDateTime.parse("2026-09-01T08:00:00+01:00")
        );

        assertThatThrownBy(() -> service.create(request))
                .isInstanceOf(InvalidBusinessStateException.class)
                .hasMessageContaining("结束时间必须晚于开始时间");
    }
}
