package com.example.locallife.memory;

import com.example.locallife.common.BusinessConflictException;
import com.example.locallife.common.InvalidBusinessStateException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import org.springframework.web.server.ResponseStatusException;

import java.time.Clock;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.TreeMap;
import java.util.UUID;

/** Catalog-bound V13 authority. No model or browser payload can invent a tuple. */
@Service
public class ShoppingMemoryV3Service {
    private static final int EXPIRY_DAYS = 180;
    private final MemoryMapper mapper;
    private final ObjectMapper json;
    private final Clock clock;
    private final ShoppingMemoryProjectionCache cache;

    @Autowired
    public ShoppingMemoryV3Service(
            MemoryMapper mapper,
            ObjectMapper json,
            ShoppingMemoryProjectionCache cache
    ) {
        this(mapper, json, Clock.systemUTC(), cache);
    }

    ShoppingMemoryV3Service(
            MemoryMapper mapper,
            ObjectMapper json,
            Clock clock,
            ShoppingMemoryProjectionCache cache
    ) {
        this.mapper = mapper;
        this.json = json;
        this.clock = clock;
        this.cache = cache;
    }

    @Transactional
    public MemoryConsentGrant issueConsent(
            String authenticatedOwner,
            MemoryConsentIssueV3Request request
    ) {
        String owner = text(authenticatedOwner);
        if (request == null || request.preference() == null) {
            throw new InvalidBusinessStateException("memory consent is invalid");
        }
        String operation = operation(request.operation());
        MemoryPreferenceV3 preference = request.preference();
        validatePreference(preference, isWithdrawal(operation));
        String action = consentAction(operation);
        if (!action.equals(request.consentAction())) {
            throw new InvalidBusinessStateException("memory consent action is invalid");
        }
        String commandId = UUID.randomUUID().toString();
        String eventId = UUID.randomUUID().toString();
        String contentDigest = contentDigest(preference);
        String commandDigest = commandDigest(
                commandId, operation, request.predecessorId(),
                request.previousVersion(), contentDigest
        );
        LocalDateTime expiresAt = LocalDateTime.now(clock).plusMinutes(5);
        mapper.issueV2Consent(
                eventId, owner, commandId, action, commandDigest,
                contentDigest, expiresAt
        );
        return new MemoryConsentGrant(
                commandId, eventId, action,
                expiresAt.toInstant(ZoneOffset.UTC)
        );
    }

    @Transactional(readOnly = true)
    public CatalogPreferenceValidationResponse validateCandidate(
            String authenticatedOwner,
            MemoryPreferenceV3 preference
    ) {
        text(authenticatedOwner);
        validatePreference(preference, false);
        String label = mapper.catalogValueLabel(
                preference.catalogRevision(), preference.categoryId(),
                preference.attributeKey(), preference.normalizedValue()
        );
        if (label == null || label.isBlank() || label.length() > 256) {
            throw new InvalidBusinessStateException("memory catalog tuple is invalid");
        }
        return new CatalogPreferenceValidationResponse(true, label);
    }

    @Transactional
    public V3MemoryResponse write(
            String authenticatedOwner,
            MemoryCommandV3Request request
    ) {
        String owner = text(authenticatedOwner);
        if (request == null || request.preference() == null) {
            throw new InvalidBusinessStateException("memory command is invalid");
        }
        String commandId = text(request.commandId());
        String eventId = text(request.consentEventId());
        String operation = operation(request.operation());
        boolean withdrawal = isWithdrawal(operation);
        MemoryPreferenceV3 preference = request.preference();
        validatePreference(preference, withdrawal);
        String contentDigest = contentDigest(preference);
        String commandDigest = commandDigest(
                commandId, operation, request.predecessorId(),
                request.previousVersion(), contentDigest
        );
        String requestDigest = ShoppingMemoryService.digest(Map.of(
                "commandDigest", commandDigest,
                "consentEventId", eventId
        ));

        int reserved = mapper.reserveV2Command(
                owner, commandId, requestDigest, operation
        );
        MemoryCommandResult ledger = mapper.v2CommandResultForUpdate(owner, commandId);
        if (ledger == null || !requestDigest.equals(ledger.requestDigest())
                || !operation.equals(ledger.operation())) {
            throw new BusinessConflictException("memory command id conflict");
        }
        if (reserved == 0) {
            if ("APPLIED".equals(ledger.status())) {
                return V3MemoryResponse.from(ledger);
            }
            throw unavailable("memory command outcome unavailable");
        }
        if (reserved != 1 || !"PROCESSING".equals(ledger.status())) {
            throw unavailable("memory command reservation unavailable");
        }

        String action = consentAction(operation);
        if (mapper.consumeV2Consent(
                eventId, owner, commandId, action, commandDigest, contentDigest
        ) != 1) {
            throw new BusinessConflictException("memory consent is unavailable");
        }

        CatalogPreferenceMemory previous = mapper.latestV3ForUpdate(
                owner, preference.categoryId(), preference.recipientScope(),
                preference.attributeKey()
        );
        int version;
        String status;
        String supersedes;
        if ("write".equals(operation)) {
            if (previous == null) {
                version = 1;
                supersedes = null;
            } else if (reopenableTerminal(owner, previous, preference)) {
                // "Forget" and "disable" stop the old record from affecting
                // recommendations, but must not permanently ban the user from
                // explicitly remembering the same logical preference later.
                // Re-open as the next auditable chain version.
                version = previous.version() + 1;
                supersedes = previous.id();
            } else {
                throw new BusinessConflictException("memory logical key already exists");
            }
            status = "ACTIVE";
        } else {
            if (!validPredecessor(owner, previous, request, preference)) {
                throw new BusinessConflictException("memory predecessor conflict");
            }
            version = previous.version() + 1;
            supersedes = previous.id();
            status = withdrawal
                    ? ("revoke".equals(operation) ? "REVOKED" : "SUPPRESSED")
                    : "ACTIVE";
        }

        LocalDateTime now = LocalDateTime.now(clock);
        CatalogMemoryWrite created = new CatalogMemoryWrite(
                UUID.randomUUID().toString(), owner, preference.categoryId(),
                preference.recipientScope(), preference.source(),
                preference.preferenceKind(), preference.catalogRevision(),
                preference.attributeKey(), preference.normalizedValue(),
                version, status, now.plusDays(EXPIRY_DAYS), supersedes,
                commandDigest, contentDigest
        );
        mapper.insertV3(created);
        mapper.audit(owner, created.id(), operation, commandDigest, eventId);
        mapper.advanceProjectionHead(owner);
        if (mapper.completeV2Command(
                owner, commandId, requestDigest, created.id(), version, status
        ) != 1) {
            throw new IllegalStateException("memory command completion unavailable");
        }
        if (cache != null) {
            TransactionSynchronizationManager.registerSynchronization(
                    new TransactionSynchronization() {
                        @Override public void afterCommit() { cache.evict(owner); }
                    }
            );
        }
        return V3MemoryResponse.of(created);
    }

    @Transactional(readOnly = true)
    public MemoryProjectionV3Response projection(String authenticatedOwner) {
        String owner = text(authenticatedOwner);
        final Long revision;
        try {
            revision = mapper.projectionRevision(owner);
        } catch (Exception unavailable) {
            throw unavailable("memory authority unavailable");
        }
        String ownerBinding = MemoryProjectionV2Response.bindOwner(owner);
        if (revision == null) {
            return new MemoryProjectionV3Response(3, 0, ownerBinding, false, List.of());
        }
        List<CatalogPreferenceMemory> candidates = mapper.projectionV3Candidates(
                owner, LocalDateTime.now(clock)
        );
        if (candidates.size() > 64) {
            throw unavailable("memory projection cardinality unavailable");
        }
        List<MemoryProjectionV3Response.Entry> verified = new ArrayList<>();
        for (CatalogPreferenceMemory candidate : candidates) {
            List<CatalogPreferenceMemory> chain = mapper.projectionV3Chain(
                    owner, candidate.categoryId(), candidate.recipientScope(),
                    candidate.attributeKey()
            );
            if (validCandidate(owner, candidate) && verifiedChain(candidate, chain)) {
                verified.add(entry(candidate));
            }
        }
        boolean truncated = verified.size() > 8;
        List<MemoryProjectionV3Response.Entry> selected = List.copyOf(
                verified.subList(0, Math.min(8, verified.size()))
        );
        try {
            if (json.writeValueAsBytes(selected).length > 4096) {
                throw new InvalidBusinessStateException("memory projection v3 exceeds budget");
            }
        } catch (InvalidBusinessStateException invalid) {
            throw invalid;
        } catch (Exception impossible) {
            throw new IllegalStateException(impossible);
        }
        return new MemoryProjectionV3Response(
                3, revision, ownerBinding, truncated, selected
        );
    }

    private void validatePreference(MemoryPreferenceV3 preference, boolean withdrawal) {
        if (preference == null
                || !preference.categoryId().matches("[a-z0-9][a-z0-9_-]{0,63}")
                || !Set.of("prefer", "avoid", "indifferent").contains(preference.preferenceKind())
                || !preference.attributeKey().matches("[a-z][a-z0-9_]{0,63}")
                || !preference.normalizedValue().matches("[a-z0-9][a-z0-9._:-]{0,127}")
                || !preference.catalogRevision().matches("[A-Za-z0-9._:-]{1,64}")
                || !"self".equals(preference.recipientScope())
                || !"user_confirmed".equals(preference.source())) {
            throw new InvalidBusinessStateException("memory preference is invalid");
        }
        if (!withdrawal && mapper.catalogValueExists(
                preference.catalogRevision(), preference.categoryId(),
                preference.attributeKey(), preference.normalizedValue()
        ) != 1) {
            throw new InvalidBusinessStateException("memory catalog tuple is invalid");
        }
    }

    private boolean validCandidate(String owner, CatalogPreferenceMemory item) {
        return item != null
                && owner.equals(item.ownerUserId())
                && item.schemaVersion() == 3
                && "self".equals(item.recipientScope())
                && "user_confirmed".equals(item.source())
                && Set.of("prefer", "avoid", "indifferent").contains(item.preferenceKind())
                && "ACTIVE".equals(item.status())
                && item.createdAt() != null && item.updatedAt() != null
                && item.expiresAt() != null
                && !item.updatedAt().isBefore(item.createdAt())
                && item.expiresAt().isAfter(item.updatedAt())
                && item.expiresAt().isAfter(LocalDateTime.now(clock))
                && mapper.catalogValueExists(
                        item.catalogRevision(), item.categoryId(),
                        item.attributeKey(), item.normalizedValue()
                ) == 1;
    }

    private static boolean verifiedChain(
            CatalogPreferenceMemory terminal,
            List<CatalogPreferenceMemory> chain
    ) {
        if (chain == null || chain.isEmpty() || chain.size() != terminal.version()) {
            return false;
        }
        for (int index = 0; index < chain.size(); index++) {
            CatalogPreferenceMemory item = chain.get(index);
            if (item == null || item.schemaVersion() != 3 || item.version() != index + 1
                    || !terminal.ownerUserId().equals(item.ownerUserId())
                    || !terminal.categoryId().equals(item.categoryId())
                    || !terminal.recipientScope().equals(item.recipientScope())
                    || !terminal.attributeKey().equals(item.attributeKey())
                    || !"user_confirmed".equals(item.source())
                    || !Set.of("ACTIVE", "REVOKED", "SUPPRESSED").contains(item.status())
                    || item.createdAt() == null || item.updatedAt() == null
                    || item.expiresAt() == null || item.id() == null || item.id().isBlank()) {
                return false;
            }
            if (index == 0 ? item.supersedesId() != null
                    : !Objects.equals(item.supersedesId(), chain.get(index - 1).id())) {
                return false;
            }
        }
        return terminal.id().equals(chain.get(chain.size() - 1).id())
                && "ACTIVE".equals(terminal.status());
    }

    private static MemoryProjectionV3Response.Entry entry(CatalogPreferenceMemory item) {
        return new MemoryProjectionV3Response.Entry(
                item.id(), item.categoryId(), item.recipientScope(),
                item.preferenceKind(), item.attributeKey(), item.normalizedValue(),
                item.catalogRevision(), item.version(), item.status(),
                item.createdAt().toInstant(ZoneOffset.UTC),
                item.updatedAt().toInstant(ZoneOffset.UTC),
                item.expiresAt().toInstant(ZoneOffset.UTC),
                item.supersedesId(), true
        );
    }

    private boolean validPredecessor(
            String owner,
            CatalogPreferenceMemory previous,
            MemoryCommandV3Request request,
            MemoryPreferenceV3 preference
    ) {
        return previous != null
                && request.predecessorId() != null
                && request.previousVersion() != null
                && previous.id().equals(request.predecessorId())
                && previous.version() == request.previousVersion()
                && "ACTIVE".equals(previous.status())
                && previous.expiresAt() != null
                && previous.expiresAt().isAfter(LocalDateTime.now(clock))
                && owner.equals(previous.ownerUserId())
                && previous.categoryId().equals(preference.categoryId())
                && previous.recipientScope().equals(preference.recipientScope())
                && previous.attributeKey().equals(preference.attributeKey())
                && previous.source().equals(preference.source());
    }

    private boolean reopenableTerminal(
            String owner,
            CatalogPreferenceMemory previous,
            MemoryPreferenceV3 preference
    ) {
        boolean withdrawn = Set.of("REVOKED", "SUPPRESSED")
                .contains(previous.status());
        boolean expired = "ACTIVE".equals(previous.status())
                && previous.expiresAt() != null
                && !previous.expiresAt().isAfter(LocalDateTime.now(clock));
        return (withdrawn || expired)
                && previous.id() != null && !previous.id().isBlank()
                && previous.version() > 0
                && owner.equals(previous.ownerUserId())
                && preference.categoryId().equals(previous.categoryId())
                && preference.recipientScope().equals(previous.recipientScope())
                && preference.attributeKey().equals(previous.attributeKey())
                && preference.source().equals(previous.source());
    }

    private static String contentDigest(MemoryPreferenceV3 preference) {
        return ShoppingMemoryService.digest(Map.of(
                "schemaVersion", 3,
                "categoryId", preference.categoryId(),
                "preferenceKind", preference.preferenceKind(),
                "attributeKey", preference.attributeKey(),
                "normalizedValue", preference.normalizedValue(),
                "catalogRevision", preference.catalogRevision(),
                "recipientScope", preference.recipientScope(),
                "source", preference.source()
        ));
    }

    private static String commandDigest(
            String commandId,
            String operation,
            String predecessorId,
            Integer previousVersion,
            String contentDigest
    ) {
        Map<String, Object> command = new TreeMap<>();
        command.put("commandId", commandId);
        command.put("operation", operation);
        command.put("predecessorId", predecessorId);
        command.put("previousVersion", previousVersion);
        command.put("contentDigest", contentDigest);
        return ShoppingMemoryService.digest(command);
    }

    private static String operation(String value) {
        if (!Set.of("write", "update", "revoke", "suppress").contains(value)) {
            throw new InvalidBusinessStateException("memory command is invalid");
        }
        return value;
    }

    private static boolean isWithdrawal(String operation) {
        return "revoke".equals(operation) || "suppress".equals(operation);
    }

    private static String consentAction(String operation) {
        if ("revoke".equals(operation)) return "forget";
        if ("suppress".equals(operation)) return "disable";
        return "confirm_suggestion";
    }

    private static String text(String value) {
        if (value == null || value.isBlank() || value.length() > 128) {
            throw new InvalidBusinessStateException("memory identity is invalid");
        }
        return value;
    }

    private static ResponseStatusException unavailable(String message) {
        return new ResponseStatusException(HttpStatus.SERVICE_UNAVAILABLE, message);
    }

    public record V3MemoryResponse(
            String entryId,
            int version,
            String status
    ) {
        static V3MemoryResponse of(CatalogMemoryWrite row) {
            return new V3MemoryResponse(row.id(), row.version(), row.status());
        }

        static V3MemoryResponse from(MemoryCommandResult row) {
            if (row.memoryId() == null || row.memoryVersion() == null
                    || row.memoryStatus() == null) {
                throw unavailable("memory command outcome unavailable");
            }
            return new V3MemoryResponse(
                    row.memoryId(), row.memoryVersion(), row.memoryStatus()
            );
        }
    }
}
