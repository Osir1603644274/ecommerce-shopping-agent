package com.example.locallife.memory;

import com.example.locallife.common.InvalidBusinessStateException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import java.time.Clock;
import java.time.Instant;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class ShoppingMemoryV3ServiceTests {
    private final MemoryMapper mapper = mock(MemoryMapper.class);
    private final Clock clock = Clock.fixed(
            Instant.parse("2026-08-29T00:00:00Z"), ZoneOffset.UTC
    );
    private final ShoppingMemoryV3Service service = new ShoppingMemoryV3Service(
            mapper, new ObjectMapper().findAndRegisterModules(), clock, null
    );

    @Test void catalogTupleIsRequiredBeforeConsentIsIssued() {
        MemoryConsentIssueV3Request request = consent(preference("prefer"));
        when(mapper.catalogValueExists(anyString(), anyString(), anyString(), anyString()))
                .thenReturn(0);

        assertThatThrownBy(() -> service.issueConsent("owner", request))
                .isInstanceOf(InvalidBusinessStateException.class)
                .hasMessageContaining("catalog tuple");
        verify(mapper, never()).issueV2Consent(
                anyString(), anyString(), anyString(), anyString(),
                anyString(), anyString(), any()
        );
    }

    @Test void confirmedCatalogPreferencePersistsForOneHundredEightyDays() {
        MemoryPreferenceV3 preference = preference("avoid");
        when(mapper.catalogValueExists(anyString(), anyString(), anyString(), anyString()))
                .thenReturn(1);
        when(mapper.catalogValueExists(anyString(), anyString(), anyString(), anyString()))
                .thenReturn(1);
        AtomicReference<String> requestDigest = new AtomicReference<>();
        when(mapper.reserveV2Command(eq("owner"), eq("command-1"), anyString(), eq("write")))
                .thenAnswer(call -> { requestDigest.set(call.getArgument(2)); return 1; });
        when(mapper.v2CommandResultForUpdate("owner", "command-1"))
                .thenAnswer(call -> new MemoryCommandResult(
                        "owner", "command-1", requestDigest.get(), "write",
                        "PROCESSING", null, null, null
                ));
        when(mapper.consumeV2Consent(
                eq("event-1"), eq("owner"), eq("command-1"),
                eq("confirm_suggestion"), anyString(), anyString()
        )).thenReturn(1);
        when(mapper.latestV3ForUpdate("owner", "exercise-fitness", "self", "equipment_type"))
                .thenReturn(null);
        when(mapper.completeV2Command(
                eq("owner"), eq("command-1"), anyString(), anyString(),
                eq(1), eq("ACTIVE")
        )).thenReturn(1);

        ShoppingMemoryV3Service.V3MemoryResponse result = service.write(
                "owner",
                new MemoryCommandV3Request(
                        "command-1", "write", null, null,
                        preference, "event-1"
                )
        );

        assertThat(result.version()).isEqualTo(1);
        verify(mapper).insertV3(argThat(row ->
                row.categoryId().equals("exercise-fitness")
                        && row.preferenceKind().equals("avoid")
                        && row.attributeKey().equals("equipment_type")
                        && row.normalizedValue().equals("indoor-bike")
                        && row.catalogRevision().equals("shopping-companion-v1")
                        && row.expiresAt().equals(LocalDateTime.of(2027, 2, 25, 0, 0))
        ));
    }

    @Test void explicitWriteCanReopenARevokedOrSuppressedLogicalPreference() {
        for (String terminalStatus : List.of("REVOKED", "SUPPRESSED")) {
            reset(mapper);
            MemoryPreferenceV3 preference = preference("prefer");
            when(mapper.catalogValueExists(anyString(), anyString(), anyString(), anyString()))
                    .thenReturn(1);
            AtomicReference<String> requestDigest = new AtomicReference<>();
            when(mapper.reserveV2Command(
                    eq("owner"), eq("command-1"), anyString(), eq("write")
            )).thenAnswer(call -> {
                requestDigest.set(call.getArgument(2));
                return 1;
            });
            when(mapper.v2CommandResultForUpdate("owner", "command-1"))
                    .thenAnswer(call -> new MemoryCommandResult(
                            "owner", "command-1", requestDigest.get(), "write",
                            "PROCESSING", null, null, null
                    ));
            when(mapper.consumeV2Consent(
                    eq("event-1"), eq("owner"), eq("command-1"),
                    eq("confirm_suggestion"), anyString(), anyString()
            )).thenReturn(1);
            when(mapper.latestV3ForUpdate(
                    "owner", "exercise-fitness", "self", "equipment_type"
            )).thenReturn(row("terminal-entry", "avoid", 2, "entry-1", terminalStatus));
            when(mapper.completeV2Command(
                    eq("owner"), eq("command-1"), anyString(), anyString(),
                    eq(3), eq("ACTIVE")
            )).thenReturn(1);

            ShoppingMemoryV3Service.V3MemoryResponse result = service.write(
                    "owner",
                    new MemoryCommandV3Request(
                            "command-1", "write", null, null,
                            preference, "event-1"
                    )
            );

            assertThat(result.version()).isEqualTo(3);
            verify(mapper).insertV3(argThat(row ->
                    row.version() == 3
                            && "terminal-entry".equals(row.supersedesId())
                            && "ACTIVE".equals(row.status())
            ));
        }
    }

    @Test void projectionPublishesOnlyVerifiedCatalogBoundActiveChains() {
        CatalogPreferenceMemory candidate = row(
                "entry-1", "prefer", 1, null, "ACTIVE"
        );
        when(mapper.projectionRevision("owner")).thenReturn(4L);
        when(mapper.projectionV3Candidates(eq("owner"), any())).thenReturn(List.of(candidate));
        when(mapper.projectionV3Chain(
                "owner", "exercise-fitness", "self", "equipment_type"
        )).thenReturn(List.of(candidate));
        when(mapper.catalogValueExists(
                "shopping-companion-v1", "exercise-fitness",
                "equipment_type", "indoor-bike"
        )).thenReturn(1);

        MemoryProjectionV3Response projection = service.projection("owner");

        assertThat(projection.schemaVersion()).isEqualTo(3);
        assertThat(projection.entries()).hasSize(1);
        assertThat(projection.entries().get(0).preferenceKind()).isEqualTo("prefer");
        assertThat(projection.entries().get(0).chainVerified()).isTrue();
    }

    @Test void revokeRemainsPossibleAfterCatalogRevisionIsDeactivated() {
        MemoryPreferenceV3 preference = preference("prefer");
        when(mapper.catalogValueExists(anyString(), anyString(), anyString(), anyString()))
                .thenReturn(0);

        service.issueConsent(
                "owner",
                new MemoryConsentIssueV3Request(
                        "revoke", "entry-1", 1, preference, "forget"
                )
        );

        verify(mapper).issueV2Consent(
                anyString(), eq("owner"), anyString(), eq("forget"),
                anyString(), anyString(), any()
        );
    }

    private MemoryConsentIssueV3Request consent(MemoryPreferenceV3 preference) {
        return new MemoryConsentIssueV3Request(
                "write", null, null, preference, "confirm_suggestion"
        );
    }

    private MemoryPreferenceV3 preference(String kind) {
        return new MemoryPreferenceV3(
                "exercise-fitness", kind, "equipment_type", "indoor-bike",
                "shopping-companion-v1", "self", "user_confirmed"
        );
    }

    private CatalogPreferenceMemory row(
            String id, String kind, int version, String supersedes, String status
    ) {
        return new CatalogPreferenceMemory(
                id, "owner", "exercise-fitness", "self", "user_confirmed",
                3, kind, "shopping-companion-v1", "equipment_type",
                "indoor-bike", version, status,
                LocalDateTime.of(2026, 8, 1, 0, 0),
                LocalDateTime.of(2026, 8, 20, 0, 0),
                LocalDateTime.of(2027, 2, 16, 0, 0), supersedes
        );
    }
}
