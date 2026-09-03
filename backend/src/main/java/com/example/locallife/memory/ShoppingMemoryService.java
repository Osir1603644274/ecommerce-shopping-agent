package com.example.locallife.memory;

import com.example.locallife.common.BusinessConflictException;
import com.example.locallife.common.InvalidBusinessStateException;
import com.example.locallife.integration.OutboxService;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import org.springframework.web.server.ResponseStatusException;
import org.springframework.http.HttpStatus;

import java.security.MessageDigest;
import java.time.Clock;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.*;

@Service
public class ShoppingMemoryService {
    private static final Map<String, Map<String, Set<String>>> TOKENS = Map.of(
            "shopping_preference", Map.ofEntries(
                    Map.entry("avoid_brand", Set.of("apple","huawei","xiaomi","oppo","vivo","honor","samsung","brand-a","brand-b","brand-x","brand-y")),
                    Map.entry("brand", Set.of("apple","huawei","xiaomi","oppo","vivo","honor","samsung")),
                    Map.entry("os", Set.of("ios","android")),
                    Map.entry("battery_health", Set.of("lt70","70_80","80_90","90_plus")),
                    Map.entry("screen_originality", Set.of("original","non_original")),
                    Map.entry("motherboard_repair", Set.of("not_repaired","repaired")),
                    Map.entry("battery_originality", Set.of("original","non_original")),
                    Map.entry("scratch_level", Set.of("none","light","obvious")),
                    Map.entry("shell_condition", Set.of("normal","damaged")),
                    Map.entry("prefer_attribute", Set.of("budget","compact","waterproof")),
                    Map.entry("prefer_brand", Set.of("apple","samsung")),
                    Map.entry("prefer_category", Set.of("phone","tablet")),
                    Map.entry("prefer_price", Set.of("budget","premium"))
            ));
    private final MemoryMapper mapper; private final OutboxService outbox; private final ObjectMapper json; private final Clock clock; private final ShoppingMemoryProjectionCache cache;
    @Autowired
    public ShoppingMemoryService(MemoryMapper mapper, OutboxService outbox, ObjectMapper json, ShoppingMemoryProjectionCache cache) { this(mapper, outbox, json, Clock.systemUTC(), cache); }
    ShoppingMemoryService(MemoryMapper mapper, OutboxService outbox, ObjectMapper json, Clock clock) { this(mapper,outbox,json,clock,null); }
    ShoppingMemoryService(MemoryMapper mapper, OutboxService outbox, ObjectMapper json, Clock clock, ShoppingMemoryProjectionCache cache) { this.mapper=mapper; this.outbox=outbox; this.json=json; this.clock=clock; this.cache=cache; }

    @Transactional
    public MemoryResponse write(String authenticatedOwner, MemoryWriteRequest request) {
        String owner = text(authenticatedOwner); // JWT subject only; body has no owner field.
        if (request == null || request.consent() == null) throw new InvalidBusinessStateException("memory command is invalid");
        String operation = text(request.operation());
        if (!Set.of("write", "update", "revoke", "suppress").contains(operation)) throw new InvalidBusinessStateException("memory command is invalid");
        text(request.commandId());
        MemoryWriteRequest.MemoryPreference preference = request.preference();
        if (preference == null) throw new InvalidBusinessStateException("memory command is invalid");
        validatePreference(preference);
        String content = digest(Map.of("category",preference.category(),"semanticKey",preference.semanticKey(),"value",preference.value()));
        // Owner is intentionally excluded: clients cannot submit an owner ID.
        // The durable consent row binds this digest to the authenticated owner.
        Map<String,Object> command = new TreeMap<>(); command.put("commandId",request.commandId()); command.put("operation",operation); command.put("predecessorId",request.predecessorId()); command.put("previousVersion",request.previousVersion()); command.put("contentDigest",content);
        String commandDigest = digest(command);
        if (!content.equals(request.consent().contentDigest()) || !commandDigest.equals(request.consent().commandDigest())) throw new InvalidBusinessStateException("memory command digest mismatch");
        boolean withdrawal = operation.equals("revoke") || operation.equals("suppress");
        if (!(withdrawal ? "withdraw" : "grant").equals(request.consent().action())) throw new InvalidBusinessStateException("memory consent action is invalid");
        ShoppingMemory previous = mapper.latestForUpdate(owner, preference.category(), preference.semanticKey());
        int version; String status; String supersedes;
        if (operation.equals("write")) {
            if (previous != null) throw new BusinessConflictException("memory logical key already exists");
            version=1; status="ACTIVE"; supersedes=null;
        } else {
            if (previous == null || !previous.id().equals(request.predecessorId()) || request.previousVersion()==null || previous.version()!=request.previousVersion() || !"ACTIVE".equals(previous.status()) || !previous.expiresAt().isAfter(LocalDateTime.now(clock))) throw new BusinessConflictException("memory predecessor conflict");
            version=previous.version()+1; supersedes=previous.id(); status=withdrawal ? (operation.equals("revoke") ? "REVOKED" : "SUPPRESSED") : "ACTIVE";
        }
        try { mapper.consumeConsent(owner, text(request.consent().eventId()), commandDigest, content); } catch (DuplicateKeyException duplicate) { throw new BusinessConflictException("memory consent already consumed"); }
        String id = UUID.randomUUID().toString(); LocalDateTime now=LocalDateTime.now(clock);
        ShoppingMemory created=new ShoppingMemory(id,owner,preference.category(),preference.semanticKey(),preference.value(),version,status,now.plusDays(30),supersedes,commandDigest,content);
        mapper.insert(created); mapper.audit(owner,id,operation,commandDigest,request.consent().eventId()); mapper.advanceProjectionHead(owner);
        outbox.append("SHOPPING_MEMORY", id, "memory.changed.v1", Map.of("memoryId",id,"status",status,"version",version));
        if (cache != null) TransactionSynchronizationManager.registerSynchronization(new TransactionSynchronization() { @Override public void afterCommit() { cache.evict(owner); } });
        return MemoryResponse.of(created);
    }

    /**
     * Creates a short-lived server-owned authorization for one explicit user
     * action.  This intentionally does not write a preference yet: the follow
     * up command is idempotent and can be retried after a browser/network loss.
     */
    @Transactional
    public MemoryConsentGrant issueV2Consent(
            String authenticatedOwner,
            MemoryConsentIssueRequest request
    ) {
        String owner = text(authenticatedOwner);
        if (request == null || request.preference() == null) {
            throw new InvalidBusinessStateException("memory consent is invalid");
        }
        String operation = v2Operation(request.operation());
        MemoryPreferenceV2 preference = request.preference();
        validateV2Preference(preference);
        String action = expectedConsentAction(operation, preference.source());
        if (!action.equals(request.consentAction())) {
            throw new InvalidBusinessStateException("memory consent action is invalid");
        }
        String commandId = UUID.randomUUID().toString();
        String eventId = UUID.randomUUID().toString();
        String contentDigest = v2ContentDigest(preference);
        String commandDigest = v2CommandDigest(
                commandId, operation, request.predecessorId(),
                request.previousVersion(), contentDigest
        );
        LocalDateTime expiresAt = LocalDateTime.now(clock).plusMinutes(5);
        mapper.issueV2Consent(
                eventId, owner, commandId, action, commandDigest, contentDigest,
                expiresAt
        );
        return new MemoryConsentGrant(
                commandId, eventId, action, expiresAt.toInstant(ZoneOffset.UTC)
        );
    }

    /**
     * Applies only a matching server-issued consent.  An exact retry returns
     * the prior result before consuming the one-shot consent a second time.
     */
    @Transactional
    public V2MemoryResponse writeV2(
            String authenticatedOwner,
            MemoryCommandV2Request request
    ) {
        String owner = text(authenticatedOwner);
        if (request == null || request.preference() == null) {
            throw new InvalidBusinessStateException("memory command is invalid");
        }
        String commandId = text(request.commandId());
        String operation = v2Operation(request.operation());
        String eventId = text(request.consentEventId());
        MemoryPreferenceV2 preference = request.preference();
        validateV2Preference(preference);
        String contentDigest = v2ContentDigest(preference);
        String commandDigest = v2CommandDigest(
                commandId, operation, request.predecessorId(),
                request.previousVersion(), contentDigest
        );
        String requestDigest = digest(Map.of(
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
                return V2MemoryResponse.from(ledger);
            }
            throw new ResponseStatusException(
                    HttpStatus.SERVICE_UNAVAILABLE,
                    "memory command outcome unavailable"
            );
        }
        if (reserved != 1 || !"PROCESSING".equals(ledger.status())) {
            throw new ResponseStatusException(
                    HttpStatus.SERVICE_UNAVAILABLE,
                    "memory command reservation unavailable"
            );
        }

        String action = expectedConsentAction(operation, preference.source());
        if (mapper.consumeV2Consent(
                eventId, owner, commandId, action, commandDigest, contentDigest
        ) != 1) {
            throw new BusinessConflictException("memory consent is unavailable");
        }

        ExplicitPreferenceMemory previous = mapper.latestV2ForUpdate(
                owner, preference.productCategory(), preference.recipientScope(),
                preference.semanticKey()
        );
        int version;
        String status;
        String supersedes;
        boolean withdrawal = operation.equals("revoke") || operation.equals("suppress");
        if (operation.equals("write")) {
            if (previous != null) {
                throw new BusinessConflictException("memory logical key already exists");
            }
            version = 1;
            status = "ACTIVE";
            supersedes = null;
        } else {
            if (!validV2Predecessor(owner, previous, request, preference)) {
                throw new BusinessConflictException("memory predecessor conflict");
            }
            version = previous.version() + 1;
            supersedes = previous.id();
            status = withdrawal
                    ? (operation.equals("revoke") ? "REVOKED" : "SUPPRESSED")
                    : "ACTIVE";
        }

        LocalDateTime now = LocalDateTime.now(clock);
        ExplicitMemoryWrite created = new ExplicitMemoryWrite(
                UUID.randomUUID().toString(), owner, preference.productCategory(),
                preference.recipientScope(), preference.source(),
                preference.semanticKey(), preference.value(), version, status,
                now.plusDays(30), supersedes, commandDigest, contentDigest
        );
        mapper.insertV2(created);
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
        return V2MemoryResponse.of(created);
    }
    @Transactional(readOnly = true)
    public List<MemoryResponse> projection(String authenticatedOwner) {
        return projectionEnvelope(authenticatedOwner).entries();
    }
    @Transactional(readOnly = true)
    public MemoryProjectionResponse projectionEnvelope(String authenticatedOwner) {
        String owner=text(authenticatedOwner); final Long revision;
        try { revision=mapper.projectionRevision(owner); } catch(Exception unavailable) { throw new ResponseStatusException(HttpStatus.SERVICE_UNAVAILABLE,"memory authority unavailable"); }
        // Absence is the authoritative initial state, never permission to use a
        // possibly stale Redis value from an earlier lifecycle.
        if (revision==null) return new MemoryProjectionResponse(1,0,List.of());
        List<MemoryResponse> result=cache == null ? null : cache.get(owner,revision);
        if(result == null) { result=mapper.projection(owner, LocalDateTime.now(clock)).stream().map(MemoryResponse::of).toList(); if(cache != null) cache.put(owner,revision,result); }
        try {
            if (json.writeValueAsBytes(result).length > 4096) throw new InvalidBusinessStateException("memory projection exceeds budget");
            for (MemoryResponse item : result) if (json.writeValueAsBytes(item).length > 512) throw new InvalidBusinessStateException("memory projection exceeds budget");
        } catch (Exception impossible) { throw new IllegalStateException(impossible); }
        return new MemoryProjectionResponse(1,revision,result);
    }

    @Transactional(readOnly = true)
    public MemoryProjectionV2Response projectionV2Envelope(String authenticatedOwner) {
        String owner=text(authenticatedOwner); final Long revision;
        try { revision=mapper.projectionRevision(owner); }
        catch(Exception unavailable) { throw new ResponseStatusException(HttpStatus.SERVICE_UNAVAILABLE,"memory authority unavailable"); }
        String ownerBinding=MemoryProjectionV2Response.bindOwner(owner);
        if(revision==null) return new MemoryProjectionV2Response(2,0,ownerBinding,false,List.of());
        MemoryProjectionV2Response cached=cache==null ? null : cache.getV2(owner,revision);
        MemoryProjectionV2Response authoritative=buildProjectionV2(owner,revision,ownerBinding);
        if(cached!=null && ownerBinding.equals(cached.ownerBinding()) && cached.equals(authoritative)) return cached;
        if(cache!=null) cache.evict(owner);
        if(cache!=null) cache.putV2(owner,authoritative);
        return authoritative;
    }

    private MemoryProjectionV2Response buildProjectionV2(String owner, long revision, String ownerBinding) {
        List<ExplicitPreferenceMemory> candidates=mapper.projectionV2Candidates(owner,LocalDateTime.now(clock));
        if(candidates.size()>64) throw new ResponseStatusException(HttpStatus.SERVICE_UNAVAILABLE,"memory projection cardinality unavailable");
        List<MemoryProjectionV2Response.Entry> verified=new ArrayList<>();
        for(ExplicitPreferenceMemory candidate:candidates) {
            if(!validV2Candidate(owner,candidate) || !verifiedChain(candidate,mapper.projectionV2Chain(
                    owner,candidate.memoryCategory(),candidate.productCategory(),candidate.recipientScope(),candidate.semanticKey()))) continue;
            verified.add(v2Entry(candidate));
        }
        boolean truncated=verified.size()>8;
        List<MemoryProjectionV2Response.Entry> selected=List.copyOf(verified.subList(0,Math.min(8,verified.size())));
        MemoryProjectionV2Response response=new MemoryProjectionV2Response(2,revision,ownerBinding,truncated,selected);
        try {
            if(json.writeValueAsBytes(selected).length>8192 || selected.stream().anyMatch(item -> serializedBytes(item)>1024))
                throw new InvalidBusinessStateException("memory projection v2 exceeds budget");
        } catch(InvalidBusinessStateException invalid) { throw invalid; }
        catch(Exception impossible) { throw new IllegalStateException(impossible); }
        return response;
    }

    private boolean validV2Candidate(String owner, ExplicitPreferenceMemory item) {
        return item!=null && owner.equals(item.ownerUserId())
                && "shopping_preference".equals(item.memoryCategory())
                && Set.of("phone","laptop","headphones").contains(item.productCategory())
                && "self".equals(item.recipientScope())
                && Set.of("explicit_user","user_confirmed").contains(item.source())
                && "long_term_preference".equals(item.dataClass())
                && "ACTIVE".equals(item.status()) && item.createdAt()!=null && item.updatedAt()!=null && item.expiresAt()!=null
                && !item.updatedAt().isBefore(item.createdAt()) && item.expiresAt().isAfter(item.updatedAt())
                && item.expiresAt().isAfter(LocalDateTime.now(clock))
                && isControlledPreference(item.memoryCategory(),item.semanticKey(),item.tokenValue());
    }

    private static boolean verifiedChain(ExplicitPreferenceMemory terminal, List<ExplicitPreferenceMemory> chain) {
        if(chain==null || chain.isEmpty() || chain.size()!=terminal.version()) return false;
        for(int index=0;index<chain.size();index++) {
            ExplicitPreferenceMemory item=chain.get(index);
            if(item==null || item.version()!=index+1
                    || !terminal.ownerUserId().equals(item.ownerUserId())
                    || !terminal.memoryCategory().equals(item.memoryCategory())
                    || !terminal.productCategory().equals(item.productCategory())
                    || !terminal.recipientScope().equals(item.recipientScope())
                    || !terminal.semanticKey().equals(item.semanticKey())
                    || !Set.of("explicit_user","user_confirmed").contains(item.source())
                    || !"long_term_preference".equals(item.dataClass())
                    || item.id()==null || item.id().isBlank()
                    || !Set.of("ACTIVE","SUPERSEDED").contains(item.status())
                    || item.createdAt()==null || item.updatedAt()==null || item.expiresAt()==null
                    || item.updatedAt().isBefore(item.createdAt()) || !item.expiresAt().isAfter(item.updatedAt())
                    || !isControlledPreference(item.memoryCategory(),item.semanticKey(),item.tokenValue())) return false;
            if(index==0 ? item.supersedesId()!=null : !Objects.equals(item.supersedesId(),chain.get(index-1).id())) return false;
            if(index<chain.size()-1 && Set.of("REVOKED","SUPPRESSED","EXPIRED","SUPERSEDED","LEGACY_REJECTED").contains(item.status())) return false;
        }
        return terminal.id().equals(chain.get(chain.size()-1).id()) && "ACTIVE".equals(terminal.status());
    }

    private static MemoryProjectionV2Response.Entry v2Entry(ExplicitPreferenceMemory item) {
        return new MemoryProjectionV2Response.Entry(
                item.id(),item.memoryCategory(),item.productCategory(),item.recipientScope(),
                item.semanticKey(),item.tokenValue(),item.source(),item.dataClass(),item.version(),
                item.status(),item.createdAt().toInstant(ZoneOffset.UTC),item.updatedAt().toInstant(ZoneOffset.UTC),
                item.expiresAt().toInstant(ZoneOffset.UTC),item.supersedesId(),true
        );
    }

    private int serializedBytes(Object value) { try { return json.writeValueAsBytes(value).length; } catch(Exception invalid) { return Integer.MAX_VALUE; } }
    private static void validatePreference(MemoryWriteRequest.MemoryPreference p) { if (p.category()==null || p.semanticKey()==null || p.value()==null || !p.value().chars().allMatch(ch -> ch < 128) || !p.value().equals(p.value().toLowerCase(Locale.ROOT)) || !TOKENS.getOrDefault(p.category(),Map.of()).getOrDefault(p.semanticKey(),Set.of()).contains(p.value())) throw new InvalidBusinessStateException("memory preference is invalid"); }
    private static void validateV2Preference(MemoryPreferenceV2 p) {
        if (p == null
                || !Set.of("phone", "laptop", "headphones").contains(p.productCategory())
                || !"self".equals(p.recipientScope())
                || !"explicit_user".equals(p.source())
                || !isControlledPreference("shopping_preference", p.semanticKey(), p.value())) {
            throw new InvalidBusinessStateException("memory preference is invalid");
        }
    }
    private static String v2Operation(String operation) {
        if (!Set.of("write", "update", "revoke", "suppress").contains(operation)) {
            throw new InvalidBusinessStateException("memory command is invalid");
        }
        return operation;
    }
    private static String expectedConsentAction(String operation, String source) {
        if (operation.equals("revoke")) return "forget";
        if (operation.equals("suppress")) return "disable";
        if ("explicit_user".equals(source)) return "remember";
        if ("user_confirmed".equals(source)) return "confirm_suggestion";
        throw new InvalidBusinessStateException("memory consent action is invalid");
    }
    private static String v2ContentDigest(MemoryPreferenceV2 preference) {
        return digest(Map.of(
                "memoryCategory", "shopping_preference",
                "productCategory", preference.productCategory(),
                "recipientScope", preference.recipientScope(),
                "semanticKey", preference.semanticKey(),
                "source", preference.source(),
                "value", preference.value()
        ));
    }
    private static String v2CommandDigest(
            String commandId, String operation, String predecessorId,
            Integer previousVersion, String contentDigest
    ) {
        Map<String, Object> command = new TreeMap<>();
        command.put("commandId", commandId);
        command.put("operation", operation);
        command.put("predecessorId", predecessorId);
        command.put("previousVersion", previousVersion);
        command.put("contentDigest", contentDigest);
        return digest(command);
    }
    private boolean validV2Predecessor(
            String owner,
            ExplicitPreferenceMemory previous,
            MemoryCommandV2Request request,
            MemoryPreferenceV2 preference
    ) {
        if (previous == null || request.predecessorId() == null
                || request.previousVersion() == null
                || !previous.id().equals(request.predecessorId())
                || previous.version() != request.previousVersion()
                || !"ACTIVE".equals(previous.status())
                || previous.expiresAt() == null
                || !previous.expiresAt().isAfter(LocalDateTime.now(clock))) {
            return false;
        }
        if (!owner.equals(previous.ownerUserId())
                || !"shopping_preference".equals(previous.memoryCategory())
                || !preference.productCategory().equals(previous.productCategory())
                || !preference.recipientScope().equals(previous.recipientScope())
                || !preference.semanticKey().equals(previous.semanticKey())
                || !preference.source().equals(previous.source())) {
            return false;
        }
        boolean withdrawal = request.operation().equals("revoke") || request.operation().equals("suppress");
        return !withdrawal || preference.value().equals(previous.tokenValue());
    }
    static boolean isControlledPreference(String category, String semanticKey, String value) { return category != null && semanticKey != null && value != null && value.chars().allMatch(ch -> ch < 128) && value.equals(value.toLowerCase(Locale.ROOT)) && TOKENS.getOrDefault(category,Map.of()).getOrDefault(semanticKey,Set.of()).contains(value); }
    public static String digest(Object value) { try { return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(canonicalJson(value))); } catch (Exception error) { throw new IllegalStateException("memory canonicalization failed",error); } }
    static byte[] canonicalJson(Object value) {
        StringBuilder out = new StringBuilder(); canonical(value, out); return out.toString().getBytes(java.nio.charset.StandardCharsets.UTF_8);
    }
    @SuppressWarnings("unchecked") private static void canonical(Object value, StringBuilder out) {
        if (value == null) { out.append("null"); return; }
        if (value instanceof String text) { out.append('"'); for (int i=0;i<text.length();i++) { char c=text.charAt(i); if (c=='"'||c=='\\') out.append('\\'); if (c<0x20) out.append(String.format("\\u%04x", (int)c)); else out.append(c); } out.append('"'); return; }
        if (value instanceof Integer || value instanceof Long) { out.append(value); return; }
        if (value instanceof Map<?,?> map) { TreeMap<String,Object> sorted=new TreeMap<>(); for (var item:map.entrySet()) { if (item.getKey() == null || item.getKey().getClass()!=String.class) throw new IllegalArgumentException("invalid canonical key"); sorted.put((String)item.getKey(),item.getValue()); } out.append('{'); boolean first=true; for (var item:sorted.entrySet()) { if(!first)out.append(','); first=false; canonical(item.getKey(),out);out.append(':');canonical(item.getValue(),out); } out.append('}'); return; }
        throw new IllegalArgumentException("invalid canonical value");
    }
    private static String text(String value) { if (value==null || value.isBlank() || value.length()>128) throw new InvalidBusinessStateException("memory identity is invalid"); return value; }
    public record MemoryResponse(String entryId,String category,String semanticKey,String value,int version,String status) { static MemoryResponse of(ShoppingMemory m) { return new MemoryResponse(m.id(),m.category(),m.semanticKey(),m.tokenValue(),m.version(),m.status()); } }
    public record V2MemoryResponse(
            String entryId, int version, String status
    ) {
        static V2MemoryResponse of(ExplicitMemoryWrite row) {
            return new V2MemoryResponse(row.id(), row.version(), row.status());
        }
        static V2MemoryResponse from(MemoryCommandResult row) {
            if (row.memoryId() == null || row.memoryVersion() == null
                    || row.memoryStatus() == null) {
                throw new ResponseStatusException(
                        HttpStatus.SERVICE_UNAVAILABLE,
                        "memory command outcome unavailable"
                );
            }
            return new V2MemoryResponse(
                    row.memoryId(), row.memoryVersion(), row.memoryStatus()
            );
        }
    }
}
