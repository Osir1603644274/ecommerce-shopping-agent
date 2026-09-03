package com.example.locallife.memory;

import com.example.locallife.common.BusinessConflictException;
import com.example.locallife.common.InvalidBusinessStateException;
import com.example.locallife.integration.OutboxService;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.springframework.dao.DuplicateKeyException;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.Map;
import java.util.TreeMap;
import java.nio.file.Path;

import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import java.time.LocalDateTime;
import java.util.List;

class ShoppingMemoryServiceTests {
    private final MemoryMapper mapper = mock(MemoryMapper.class);
    private final OutboxService outbox = mock(OutboxService.class);
    private final ObjectMapper json = new ObjectMapper().findAndRegisterModules();
    private final ShoppingMemoryService service = new ShoppingMemoryService(mapper, outbox, json, Clock.fixed(Instant.parse("2026-08-22T00:00:00Z"), ZoneOffset.UTC));

    @Test void digestMismatchAndWhitelistFailBeforeAnyPersistentWrite() {
        assertThatThrownBy(() -> service.write("owner", request("bad", "unknownsecret", "0".repeat(64), "0".repeat(64))))
                .isInstanceOf(InvalidBusinessStateException.class);
        assertThatThrownBy(() -> service.write("owner", request("bad", "brand-x", "0".repeat(64), "0".repeat(64))))
                .isInstanceOf(InvalidBusinessStateException.class);
        verifyNoInteractions(mapper, outbox);
    }

    @Test void duplicateConsentAndTerminalPredecessorFailClosed() {
        MemoryWriteRequest valid = validWrite("write-1", "event-1");
        when(mapper.latestForUpdate(anyString(), anyString(), anyString())).thenReturn(null);
        doThrow(new DuplicateKeyException("duplicate")).when(mapper).consumeConsent(anyString(), anyString(), anyString(), anyString());
        assertThatThrownBy(() -> service.write("owner", valid)).isInstanceOf(BusinessConflictException.class);
        ShoppingMemory terminal = new ShoppingMemory("old","owner","shopping_preference","avoid_brand","brand-x",2,"REVOKED",java.time.LocalDateTime.now(),"before","a","b");
        when(mapper.latestForUpdate("owner", "shopping_preference", "avoid_brand")).thenReturn(terminal);
        MemoryWriteRequest update = validUpdate("update-1", "event-2", "old", 2);
        assertThatThrownBy(() -> service.write("owner", update)).isInstanceOf(BusinessConflictException.class);
        verify(outbox, never()).append(anyString(), anyString(), anyString(), any());
    }

    @Test void canonicalDigestHasIndependentGoldenAndIgnoresMapInsertionOrder() {
        Map<String,Object> first = new java.util.LinkedHashMap<>(); first.put("b", 1); first.put("a", "x");
        Map<String,Object> second = new java.util.LinkedHashMap<>(); second.put("a", "x"); second.put("b", 1);
        assertThat(ShoppingMemoryService.canonicalJson(first)).isEqualTo("{\"a\":\"x\",\"b\":1}".getBytes(StandardCharsets.UTF_8));
        assertThat(ShoppingMemoryService.digest(first)).isEqualTo("cdab067e9f3beb32d1252cfd63e492592fecbf591b0d08cadb24bb17f3864246");
        assertThat(ShoppingMemoryService.digest(second)).isEqualTo(ShoppingMemoryService.digest(first));
    }

    @Test void canonicalDigestIsStableAcrossEightIndependentJvms() throws Exception {
        String golden = "cdab067e9f3beb32d1252cfd63e492592fecbf591b0d08cadb24bb17f3864246";
        String javaBinary = System.getProperty("os.name", "").toLowerCase().contains("win")
                ? "java.exe"
                : "java";
        String java = Path.of(System.getProperty("java.home"), "bin", javaBinary).toString();
        for (int index = 0; index < 8; index++) {
            Process process = new ProcessBuilder(java, "-cp", System.getProperty("java.class.path"), DigestHelper.class.getName()).start();
            String output = new String(process.getInputStream().readAllBytes(), StandardCharsets.UTF_8).trim();
            assertThat(process.waitFor()).isZero();
            assertThat(output).isEqualTo(golden);
        }
    }

    static final class DigestHelper {
        public static void main(String[] ignored) {
            Map<String,Object> input = new java.util.LinkedHashMap<>(); input.put("b", 1); input.put("a", "x");
            System.out.print(ShoppingMemoryService.digest(input));
        }
    }

    @Test void exactExpiryPredecessorAndPersistenceFailureFailClosed() {
        ShoppingMemory expired = new ShoppingMemory("old","owner","shopping_preference","avoid_brand","brand-x",1,"ACTIVE",LocalDateTime.of(2026,8,22,0,0),null,"a","b");
        when(mapper.latestForUpdate("owner", "shopping_preference", "avoid_brand")).thenReturn(expired);
        assertThatThrownBy(() -> service.write("owner", validUpdate("update-1", "event-2", "old", 1))).isInstanceOf(BusinessConflictException.class);
        when(mapper.latestForUpdate(anyString(), anyString(), anyString())).thenReturn(null);
        doThrow(new IllegalStateException("audit down")).when(mapper).audit(anyString(), anyString(), anyString(), anyString(), anyString());
        assertThatThrownBy(() -> service.write("owner", validWrite("write-2", "event-3"))).isInstanceOf(IllegalStateException.class);
        verify(outbox, never()).append(anyString(), anyString(), anyString(), any());
    }

    @Test void projectionReturnsAtMostEightAndFailsClosedOnPerItemBudget() {
        List<ShoppingMemory> many = java.util.stream.IntStream.range(0, 9).mapToObj(i -> new ShoppingMemory("id"+i,"owner","shopping_preference","avoid_brand","brand-x",1,"ACTIVE",LocalDateTime.of(2027,1,1,0,0),null,"a","b")).toList();
        when(mapper.projection(anyString(), any())).thenReturn(many.subList(0, 8));
        assertThat(service.projection("owner")).hasSize(8);
        ShoppingMemory oversized = new ShoppingMemory("i".repeat(500),"owner","shopping_preference","avoid_brand","brand-x",1,"ACTIVE",LocalDateTime.of(2027,1,1,0,0),null,"a","b");
        when(mapper.projection(anyString(), any())).thenReturn(List.of(oversized));
        assertThatThrownBy(() -> service.projection("owner")).isInstanceOf(IllegalStateException.class);
    }

    @Test void projectionHeadMissingIsAuthoritativeEmptyAndUnavailableFailsClosed() {
        ShoppingMemoryProjectionCache cache=mock(ShoppingMemoryProjectionCache.class);
        ShoppingMemoryService guarded=new ShoppingMemoryService(mapper,outbox,json,Clock.fixed(Instant.parse("2026-08-22T00:00:00Z"),ZoneOffset.UTC),cache);
        when(mapper.projectionRevision("owner")).thenReturn(null);
        assertThat(guarded.projectionEnvelope("owner")).isEqualTo(new MemoryProjectionResponse(1,0,List.of()));
        verify(mapper,never()).projection(anyString(),any());
        verifyNoInteractions(cache);
        when(mapper.projectionRevision("owner")).thenThrow(new RuntimeException("db unavailable"));
        assertThatThrownBy(() -> guarded.projection("owner")).isInstanceOf(org.springframework.web.server.ResponseStatusException.class).hasMessageContaining("503");
        verifyNoInteractions(cache);
    }

    @Test void projectionUsesOnlyRevisionMatchedCacheOtherwiseReadsAndRefillsMysql() {
        ShoppingMemoryProjectionCache cache = mock(ShoppingMemoryProjectionCache.class);
        ShoppingMemoryService cached = new ShoppingMemoryService(mapper,outbox,json,Clock.fixed(Instant.parse("2026-08-22T00:00:00Z"), ZoneOffset.UTC),cache);
        var hit = List.of(new ShoppingMemoryService.MemoryResponse("id","shopping_preference","avoid_brand","brand-x",1,"ACTIVE"));
        when(mapper.projectionRevision("owner")).thenReturn(7L); when(cache.get("owner",7L)).thenReturn(hit);
        assertThat(cached.projection("owner")).isEqualTo(hit); verify(mapper,never()).projection(anyString(),any());
        reset(mapper,cache); when(mapper.projectionRevision("owner")).thenReturn(8L); when(cache.get("owner",8L)).thenReturn(null); when(mapper.projection(anyString(),any())).thenReturn(List.of());
        assertThat(cached.projection("owner")).isEmpty(); verify(cache).put(eq("owner"),eq(8L),anyList());
    }

    @Test void cacheEvictionIsRegisteredForAfterCommitOnly() {
        ShoppingMemoryProjectionCache cache=mock(ShoppingMemoryProjectionCache.class);
        ShoppingMemoryService cached=new ShoppingMemoryService(mapper,outbox,json,Clock.fixed(Instant.parse("2026-08-22T00:00:00Z"),ZoneOffset.UTC),cache);
        when(mapper.latestForUpdate(anyString(),anyString(),anyString())).thenReturn(null);
        org.springframework.transaction.support.TransactionSynchronizationManager.initSynchronization();
        try {
            cached.write("owner",validWrite("commit-1","event-commit"));
            var callbacks=org.springframework.transaction.support.TransactionSynchronizationManager.getSynchronizations();
            verifyNoInteractions(cache);
            callbacks.forEach(org.springframework.transaction.support.TransactionSynchronization::afterCommit);
            verify(cache).evict("owner");
        } finally { org.springframework.transaction.support.TransactionSynchronizationManager.clearSynchronization(); }
    }

    @Test void cacheEvictionIsNotCalledForRollbackPath() {
        ShoppingMemoryProjectionCache cache=mock(ShoppingMemoryProjectionCache.class);
        ShoppingMemoryService cached=new ShoppingMemoryService(mapper,outbox,json,Clock.fixed(Instant.parse("2026-08-22T00:00:00Z"),ZoneOffset.UTC),cache);
        when(mapper.latestForUpdate(anyString(),anyString(),anyString())).thenReturn(null);
        org.springframework.transaction.support.TransactionSynchronizationManager.initSynchronization();
        try { cached.write("owner",validWrite("rollback-1","event-rollback")); verifyNoInteractions(cache); }
        finally { org.springframework.transaction.support.TransactionSynchronizationManager.clearSynchronization(); }
    }

    @Test void v2ProjectionVerifiesChainsAndReportsDeterministicTruncation() {
        ShoppingMemoryProjectionCache cache=mock(ShoppingMemoryProjectionCache.class);
        ShoppingMemoryService guarded=new ShoppingMemoryService(mapper,outbox,json,Clock.fixed(Instant.parse("2026-08-22T00:00:00Z"),ZoneOffset.UTC),cache);
        when(mapper.projectionRevision("owner")).thenReturn(12L);
        when(cache.getV2("owner",12L)).thenReturn(null);
        List<ExplicitPreferenceMemory> rows=List.of(
                explicit("entry-a","os","android",1,null),
                explicit("entry-b","battery_health","90_plus",1,null),
                explicit("entry-c","screen_originality","original",1,null),
                explicit("entry-d","motherboard_repair","not_repaired",1,null),
                explicit("entry-e","battery_originality","original",1,null),
                explicit("entry-f","scratch_level","none",1,null),
                explicit("entry-g","shell_condition","normal",1,null),
                explicit("entry-h","brand","huawei",1,null),
                explicit("entry-i","avoid_brand","apple",1,null)
        );
        when(mapper.projectionV2Candidates(eq("owner"),any())).thenReturn(rows);
        when(mapper.projectionV2Chain(eq("owner"),eq("shopping_preference"),eq("phone"),eq("self"),anyString()))
                .thenAnswer(call -> rows.stream().filter(item -> item.semanticKey().equals(call.getArgument(4))).toList());
        MemoryProjectionV2Response response=guarded.projectionV2Envelope("owner");
        assertThat(response.schemaVersion()).isEqualTo(2);
        assertThat(response.entries()).extracting(MemoryProjectionV2Response.Entry::entryId)
                .containsExactly("entry-a","entry-b","entry-c","entry-d","entry-e","entry-f","entry-g","entry-h");
        assertThat(response.truncated()).isTrue();
        assertThat(response.entries()).allMatch(MemoryProjectionV2Response.Entry::chainVerified);
        verify(cache).putV2(eq("owner"),eq(response));
    }

    @Test void v2ProjectionRejectsBrokenVersionChainAndSensitiveMetadata() {
        ShoppingMemoryProjectionCache cache=mock(ShoppingMemoryProjectionCache.class);
        ShoppingMemoryService guarded=new ShoppingMemoryService(mapper,outbox,json,Clock.fixed(Instant.parse("2026-08-22T00:00:00Z"),ZoneOffset.UTC),cache);
        when(mapper.projectionRevision("owner")).thenReturn(3L);
        ExplicitPreferenceMemory second=explicit("entry-2","os","android",2,"missing");
        when(mapper.projectionV2Candidates(eq("owner"),any())).thenReturn(List.of(second));
        when(mapper.projectionV2Chain(anyString(),anyString(),anyString(),anyString(),anyString())).thenReturn(List.of(second));
        assertThat(guarded.projectionV2Envelope("owner").entries()).isEmpty();
        assertThat(ShoppingMemoryService.isControlledPreference("health","avoid_material","latex")).isFalse();
    }

    @Test void v2CacheSwapAcrossOwnersIsEvictedAndRebuiltFromMysql() {
        ShoppingMemoryProjectionCache cache=mock(ShoppingMemoryProjectionCache.class);
        ShoppingMemoryService guarded=new ShoppingMemoryService(mapper,outbox,json,Clock.fixed(Instant.parse("2026-08-22T00:00:00Z"),ZoneOffset.UTC),cache);
        ExplicitPreferenceMemory authoritative=explicit("mysql-entry","os","android",1,null);
        MemoryProjectionV2Response swapped=new MemoryProjectionV2Response(
                2,7,MemoryProjectionV2Response.bindOwner("other-owner"),false,List.of(entry(authoritative)));
        when(mapper.projectionRevision("owner")).thenReturn(7L);
        when(cache.getV2("owner",7L)).thenReturn(swapped);
        when(mapper.projectionV2Candidates(eq("owner"),any())).thenReturn(List.of(authoritative));
        when(mapper.projectionV2Chain(eq("owner"),anyString(),anyString(),anyString(),anyString())).thenReturn(List.of(authoritative));

        MemoryProjectionV2Response result=guarded.projectionV2Envelope("owner");

        assertThat(result.ownerBinding()).isEqualTo(MemoryProjectionV2Response.bindOwner("owner"));
        assertThat(result.entries()).extracting(MemoryProjectionV2Response.Entry::entryId).containsExactly("mysql-entry");
        verify(cache).evict("owner");
        verify(cache).putV2("owner",result);
    }

    @Test void v2ConsentIsServerIssuedAndBindsTheExactExplicitAction() {
        MemoryConsentIssueRequest request = new MemoryConsentIssueRequest(
                "write", null, null, v2Preference("android", "explicit_user"), "remember"
        );
        MemoryConsentGrant grant = service.issueV2Consent("owner", request);

        assertThat(grant.commandId()).matches("[A-Za-z0-9_-]{1,64}");
        assertThat(grant.consentEventId()).matches("[A-Za-z0-9_-]{1,64}");
        assertThat(grant.consentAction()).isEqualTo("remember");
        assertThat(grant.expiresAt()).isEqualTo(Instant.parse("2026-08-22T00:05:00Z"));
        verify(mapper).issueV2Consent(
                eq(grant.consentEventId()), eq("owner"), eq(grant.commandId()),
                eq("remember"), anyString(), anyString(), any()
        );

        assertThatThrownBy(() -> service.issueV2Consent("owner",
                new MemoryConsentIssueRequest(
                        "write", null, null, v2Preference("android", "explicit_user"),
                        "confirm_suggestion"
                )))
                .isInstanceOf(InvalidBusinessStateException.class);
    }

    @Test void v2CommandConsumesOneIssuedGrantThenExactRetryReplaysTheResult() {
        MemoryCommandV2Request command = v2Command("v2-command-1", "v2-event-1", "write", null, null, "android", "explicit_user");
        java.util.concurrent.atomic.AtomicReference<String> requestDigest = new java.util.concurrent.atomic.AtomicReference<>();
        when(mapper.reserveV2Command(eq("owner"), eq("v2-command-1"), anyString(), eq("write")))
                .thenAnswer(call -> { requestDigest.set(call.getArgument(2)); return 1; });
        when(mapper.v2CommandResultForUpdate("owner", "v2-command-1"))
                .thenAnswer(call -> new MemoryCommandResult(
                        "owner", "v2-command-1", requestDigest.get(), "write",
                        "PROCESSING", null, null, null
                ));
        when(mapper.consumeV2Consent(anyString(), eq("owner"), eq("v2-command-1"),
                eq("remember"), anyString(), anyString())).thenReturn(1);
        when(mapper.latestV2ForUpdate("owner", "phone", "self", "os")).thenReturn(null);
        when(mapper.completeV2Command(eq("owner"), eq("v2-command-1"), anyString(),
                anyString(), eq(1), eq("ACTIVE"))).thenReturn(1);

        ShoppingMemoryService.V2MemoryResponse first = service.writeV2("owner", command);

        assertThat(first.version()).isEqualTo(1);
        assertThat(first.status()).isEqualTo("ACTIVE");
        verify(mapper).insertV2(argThat(row -> row.version() == 1
                && row.status().equals("ACTIVE")
                && row.productCategory().equals("phone")
                && row.source().equals("explicit_user")));
        verify(mapper).audit(eq("owner"), eq(first.entryId()), eq("write"), anyString(), eq("v2-event-1"));
        verifyNoInteractions(outbox);

        reset(mapper, outbox);
        String replayDigest = v2RequestDigest(command);
        when(mapper.reserveV2Command("owner", "v2-command-1", replayDigest, "write")).thenReturn(0);
        when(mapper.v2CommandResultForUpdate("owner", "v2-command-1"))
                .thenReturn(new MemoryCommandResult(
                        "owner", "v2-command-1", replayDigest, "write", "APPLIED",
                        first.entryId(), 1, "ACTIVE"
                ));

        assertThat(service.writeV2("owner", command)).isEqualTo(first);
        verify(mapper, never()).consumeV2Consent(anyString(), anyString(), anyString(), anyString(), anyString(), anyString());
        verify(mapper, never()).insertV2(any());
        verifyNoInteractions(outbox);
    }

    @Test void v2RevokeRequiresTheActivePredecessorAndPersistsATerminalVersion() {
        MemoryCommandV2Request command = v2Command("v2-command-2", "v2-event-2", "revoke", "old", 1, "android", "explicit_user");
        java.util.concurrent.atomic.AtomicReference<String> requestDigest = new java.util.concurrent.atomic.AtomicReference<>();
        when(mapper.reserveV2Command(eq("owner"), eq("v2-command-2"), anyString(), eq("revoke")))
                .thenAnswer(call -> { requestDigest.set(call.getArgument(2)); return 1; });
        when(mapper.v2CommandResultForUpdate("owner", "v2-command-2"))
                .thenAnswer(call -> new MemoryCommandResult(
                        "owner", "v2-command-2", requestDigest.get(), "revoke",
                        "PROCESSING", null, null, null
                ));
        when(mapper.consumeV2Consent(anyString(), eq("owner"), eq("v2-command-2"),
                eq("forget"), anyString(), anyString())).thenReturn(1);
        when(mapper.latestV2ForUpdate("owner", "phone", "self", "os"))
                .thenReturn(explicit("old", "os", "android", 1, null));
        when(mapper.completeV2Command(eq("owner"), eq("v2-command-2"), anyString(),
                anyString(), eq(2), eq("REVOKED"))).thenReturn(1);

        ShoppingMemoryService.V2MemoryResponse result = service.writeV2("owner", command);
        assertThat(result.entryId()).isNotBlank();
        assertThat(result.version()).isEqualTo(2);
        assertThat(result.status()).isEqualTo("REVOKED");
        verify(mapper).insertV2(argThat(row -> row.version() == 2
                && row.status().equals("REVOKED")
                && row.supersedesId().equals("old")));
    }

    @Test void v2UpdateReplacesTheSameScopedKeyWithANewActiveVersion() {
        MemoryCommandV2Request command = v2Command("v2-command-3", "v2-event-3", "update", "old", 1, "ios", "explicit_user");
        java.util.concurrent.atomic.AtomicReference<String> requestDigest = new java.util.concurrent.atomic.AtomicReference<>();
        when(mapper.reserveV2Command(eq("owner"), eq("v2-command-3"), anyString(), eq("update")))
                .thenAnswer(call -> { requestDigest.set(call.getArgument(2)); return 1; });
        when(mapper.v2CommandResultForUpdate("owner", "v2-command-3"))
                .thenAnswer(call -> new MemoryCommandResult(
                        "owner", "v2-command-3", requestDigest.get(), "update",
                        "PROCESSING", null, null, null
                ));
        when(mapper.consumeV2Consent(anyString(), eq("owner"), eq("v2-command-3"),
                eq("remember"), anyString(), anyString())).thenReturn(1);
        when(mapper.latestV2ForUpdate("owner", "phone", "self", "os"))
                .thenReturn(explicit("old", "os", "android", 1, null));
        when(mapper.completeV2Command(eq("owner"), eq("v2-command-3"), anyString(),
                anyString(), eq(2), eq("ACTIVE"))).thenReturn(1);

        ShoppingMemoryService.V2MemoryResponse result = service.writeV2("owner", command);

        assertThat(result.version()).isEqualTo(2);
        assertThat(result.status()).isEqualTo("ACTIVE");
        verify(mapper).insertV2(argThat(row -> row.version() == 2
                && row.status().equals("ACTIVE")
                && row.tokenValue().equals("ios")
                && row.supersedesId().equals("old")));
    }

    @Test void forgedChainVerifiedCacheEntryIsComparedWithCompleteMysqlProjection() {
        ShoppingMemoryProjectionCache cache=mock(ShoppingMemoryProjectionCache.class);
        ShoppingMemoryService guarded=new ShoppingMemoryService(mapper,outbox,json,Clock.fixed(Instant.parse("2026-08-22T00:00:00Z"),ZoneOffset.UTC),cache);
        ExplicitPreferenceMemory authoritative=explicit("mysql-entry","os","android",1,null);
        MemoryProjectionV2Response.Entry forged=new MemoryProjectionV2Response.Entry(
                "forged-entry","shopping_preference","phone","self","os","ios","explicit_user",
                "long_term_preference",1,"ACTIVE",Instant.parse("2026-08-01T00:00:00Z"),
                Instant.parse("2026-08-20T00:00:00Z"),Instant.parse("2026-09-20T00:00:00Z"),null,true);
        MemoryProjectionV2Response cached=new MemoryProjectionV2Response(
                2,8,MemoryProjectionV2Response.bindOwner("owner"),false,List.of(forged));
        when(mapper.projectionRevision("owner")).thenReturn(8L);
        when(cache.getV2("owner",8L)).thenReturn(cached);
        when(mapper.projectionV2Candidates(eq("owner"),any())).thenReturn(List.of(authoritative));
        when(mapper.projectionV2Chain(eq("owner"),anyString(),anyString(),anyString(),anyString())).thenReturn(List.of(authoritative));

        MemoryProjectionV2Response result=guarded.projectionV2Envelope("owner");

        assertThat(result.entries()).extracting(MemoryProjectionV2Response.Entry::entryId).containsExactly("mysql-entry");
        verify(mapper).projectionV2Chain("owner","shopping_preference","phone","self","os");
        verify(cache).evict("owner");
        verify(cache).putV2("owner",result);
    }

    private ExplicitPreferenceMemory explicit(String id,String key,String value,int version,String supersedes) {
        return new ExplicitPreferenceMemory(
                id,"owner","shopping_preference","phone","self","explicit_user","long_term_preference",
                key,value,version,"ACTIVE",LocalDateTime.of(2026,8,1,0,0),LocalDateTime.of(2026,8,20,0,0),
                LocalDateTime.of(2026,9,20,0,0),supersedes
        );
    }

    private MemoryProjectionV2Response.Entry entry(ExplicitPreferenceMemory item) {
        return new MemoryProjectionV2Response.Entry(
                item.id(),item.memoryCategory(),item.productCategory(),item.recipientScope(),item.semanticKey(),
                item.tokenValue(),item.source(),item.dataClass(),item.version(),item.status(),
                item.createdAt().toInstant(ZoneOffset.UTC),item.updatedAt().toInstant(ZoneOffset.UTC),
                item.expiresAt().toInstant(ZoneOffset.UTC),item.supersedesId(),true);
    }

    private MemoryPreferenceV2 v2Preference(String value, String source) {
        return new MemoryPreferenceV2("phone", "self", "os", value, source);
    }

    private MemoryCommandV2Request v2Command(
            String commandId, String eventId, String operation, String predecessor,
            Integer version, String value, String source
    ) {
        return new MemoryCommandV2Request(
                commandId, operation, predecessor, version,
                v2Preference(value, source), eventId
        );
    }

    private String v2RequestDigest(MemoryCommandV2Request request) {
        MemoryPreferenceV2 preference = request.preference();
        String content = sha(Map.of(
                "memoryCategory", "shopping_preference",
                "productCategory", preference.productCategory(),
                "recipientScope", preference.recipientScope(),
                "semanticKey", preference.semanticKey(),
                "source", preference.source(),
                "value", preference.value()
        ));
        TreeMap<String,Object> command = new TreeMap<>();
        command.put("commandId", request.commandId());
        command.put("operation", request.operation());
        command.put("predecessorId", request.predecessorId());
        command.put("previousVersion", request.previousVersion());
        command.put("contentDigest", content);
        return sha(Map.of(
                "commandDigest", sha(command),
                "consentEventId", request.consentEventId()
        ));
    }

    private MemoryWriteRequest validWrite(String id, String event) { return request(id, "brand-x", event, null, null, "write"); }
    private MemoryWriteRequest validUpdate(String id, String event, String predecessor, int version) { return request(id, "brand-x", event, predecessor, version, "update"); }
    private MemoryWriteRequest request(String id, String value, String commandDigest, String contentDigest) { return request(id, value, "event", null, null, "write", commandDigest, contentDigest); }
    private MemoryWriteRequest request(String id, String value, String event, String predecessor, Integer version, String operation) {
        MemoryWriteRequest.MemoryPreference pref = new MemoryWriteRequest.MemoryPreference("shopping_preference", "avoid_brand", value);
        String content = sha(Map.of("category",pref.category(),"semanticKey",pref.semanticKey(),"value",pref.value()));
        TreeMap<String,Object> command = new TreeMap<>(); command.put("commandId",id); command.put("operation",operation); command.put("predecessorId",predecessor); command.put("previousVersion",version); command.put("contentDigest",content);
        String digest = sha(command);
        return request(id,value,event,predecessor,version,operation,digest,content);
    }
    private MemoryWriteRequest request(String id, String value, String event, String predecessor, Integer version, String operation, String digest, String content) {
        return new MemoryWriteRequest(id,operation,predecessor,version,new MemoryWriteRequest.MemoryPreference("shopping_preference","avoid_brand",value),new MemoryWriteRequest.MemoryConsent(operation.equals("revoke")||operation.equals("suppress")?"withdraw":"grant",event,digest,content));
    }
    private String sha(Object value) { return ShoppingMemoryService.digest(value); }
}
