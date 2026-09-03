package com.example.locallife.memory;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.core.JsonParser;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Duration;
import java.time.Instant;
import java.time.Clock;
import java.util.HexFormat;
import java.util.List;

/** Best-effort Redis replica.  Callers must compare its revision with MySQL. */
@Component
final class ShoppingMemoryProjectionCache {
    private static final Duration TTL = Duration.ofSeconds(60);
    private final StringRedisTemplate redis; private final ObjectMapper json; private final Clock clock;
    @Autowired
    ShoppingMemoryProjectionCache(StringRedisTemplate redis, ObjectMapper json) { this(redis,json,Clock.systemUTC()); }
    ShoppingMemoryProjectionCache(StringRedisTemplate redis, ObjectMapper json, Clock clock) { this.redis=redis; this.json=json; this.clock=clock; }
    List<ShoppingMemoryService.MemoryResponse> get(String owner, long revision) {
        try {
            String raw=redis.opsForValue().get(key(owner)); if(raw==null) return null;
            JsonNode root=json.copy().enable(JsonParser.Feature.STRICT_DUPLICATE_DETECTION).readTree(raw);
            if(!root.isObject() || root.size()!=5 || !root.has("schemaVersion") || !root.has("revision") || !root.has("entries") || !root.has("createdAt") || !root.has("expiresAt") || !root.path("schemaVersion").isInt() || root.path("schemaVersion").intValue()!=1 || !(root.path("revision").isInt()||root.path("revision").isLong()) || root.path("revision").longValue()<0 || root.path("revision").longValue()!=revision || !root.path("entries").isArray() || root.path("entries").size()>8 || !root.path("createdAt").isTextual() || !root.path("expiresAt").isTextual()) return null;
            Instant created=Instant.parse(root.path("createdAt").textValue()), expires=Instant.parse(root.path("expiresAt").textValue()), now=Instant.now(clock);
            if(!root.path("createdAt").textValue().endsWith("Z") || !root.path("expiresAt").textValue().endsWith("Z") || created.isAfter(now) || !expires.isAfter(now) || !expires.isAfter(created) || expires.isAfter(created.plus(TTL))) return null;
            for(JsonNode item:root.path("entries")) if(!validEntry(item)) return null;
            List<ShoppingMemoryService.MemoryResponse> entries=json.readerForListOf(ShoppingMemoryService.MemoryResponse.class).readValue(root.path("entries"));
            if(json.writeValueAsBytes(entries).length>4096 || entries.stream().anyMatch(x -> bytes(x)>512)) return null;
            return entries;
        } catch(Exception ignored) { return null; }
    }
    void put(String owner,long revision,List<ShoppingMemoryService.MemoryResponse> entries) { try { if(entries.size()>8||json.writeValueAsBytes(entries).length>4096||entries.stream().anyMatch(x->bytes(x)>512)) return; Instant created=Instant.now(clock); redis.opsForValue().set(key(owner),json.writeValueAsString(java.util.Map.of("schemaVersion",1,"revision",revision,"entries",entries,"createdAt",created.toString(),"expiresAt",created.plus(TTL).toString())),TTL); } catch(Exception ignored) {} }

    MemoryProjectionV2Response getV2(String owner, long revision) {
        try {
            String raw=redis.opsForValue().get(keyV2(owner)); if(raw==null) return null;
            JsonNode root=json.copy().enable(JsonParser.Feature.STRICT_DUPLICATE_DETECTION).readTree(raw);
            if(!root.isObject() || root.size()!=7 || !root.path("schemaVersion").isInt() || root.path("schemaVersion").intValue()!=2
                    || !(root.path("revision").isInt()||root.path("revision").isLong())
                    || root.path("revision").longValue()<0 || root.path("revision").longValue()!=revision || !root.path("truncated").isBoolean()
                    || !root.path("ownerBinding").isTextual()
                    || !MemoryProjectionV2Response.bindOwner(owner).equals(root.path("ownerBinding").textValue())
                    || !root.path("entries").isArray() || root.path("entries").size()>8
                    || !root.path("createdAt").isTextual() || !root.path("expiresAt").isTextual()) return null;
            Instant created=Instant.parse(root.path("createdAt").textValue());
            Instant expires=Instant.parse(root.path("expiresAt").textValue());
            Instant now=Instant.now(clock);
            if(!root.path("createdAt").textValue().endsWith("Z") || !root.path("expiresAt").textValue().endsWith("Z")
                    || created.isAfter(now) || !expires.isAfter(now) || expires.isAfter(created.plus(TTL))) return null;
            for(JsonNode item:root.path("entries")) if(!validV2Entry(item,now)) return null;
            List<MemoryProjectionV2Response.Entry> entries=json.readerForListOf(MemoryProjectionV2Response.Entry.class).readValue(root.path("entries"));
            if(json.writeValueAsBytes(entries).length>8192 || entries.stream().anyMatch(x->bytes(x)>1024)) return null;
            return new MemoryProjectionV2Response(2,revision,root.path("ownerBinding").textValue(),root.path("truncated").booleanValue(),entries);
        } catch(Exception ignored) { return null; }
    }

    void putV2(String owner, MemoryProjectionV2Response response) {
        try {
            if(response.schemaVersion()!=2 || !MemoryProjectionV2Response.bindOwner(owner).equals(response.ownerBinding())
                    || response.entries().size()>8
                    || json.writeValueAsBytes(response.entries()).length>8192
                    || response.entries().stream().anyMatch(x->bytes(x)>1024)) return;
            Instant created=Instant.now(clock);
            Instant expires=response.entries().stream().map(MemoryProjectionV2Response.Entry::expiresAt)
                    .min(Instant::compareTo).map(item -> item.isBefore(created.plus(TTL)) ? item : created.plus(TTL))
                    .orElse(created.plus(TTL));
            if(!expires.isAfter(created)) return;
            redis.opsForValue().set(keyV2(owner),json.writeValueAsString(java.util.Map.of(
                    "schemaVersion",2,"revision",response.revision(),"ownerBinding",response.ownerBinding(),"truncated",response.truncated(),
                    "entries",response.entries(),"createdAt",created.toString(),"expiresAt",expires.toString()
            )),Duration.between(created,expires));
        } catch(Exception ignored) {}
    }

    void evict(String owner) { try { redis.delete(List.of(key(owner),keyV2(owner))); } catch(Exception ignored) {} }
    private int bytes(Object value) { try{return json.writeValueAsBytes(value).length;}catch(Exception e){return Integer.MAX_VALUE;} }
    private static boolean validEntry(JsonNode item) { return item.isObject() && item.size()==6 && item.has("entryId")&&item.has("category")&&item.has("semanticKey")&&item.has("value")&&item.has("version")&&item.has("status") && item.path("entryId").isTextual()&&item.path("entryId").textValue().matches("[A-Za-z0-9_-]{1,64}")&&item.path("category").isTextual()&&item.path("semanticKey").isTextual()&&item.path("value").isTextual()&&ShoppingMemoryService.isControlledPreference(item.path("category").textValue(),item.path("semanticKey").textValue(),item.path("value").textValue())&&item.path("version").isInt()&&item.path("version").intValue()>0&&item.path("status").isTextual()&&"ACTIVE".equals(item.path("status").textValue()); }
    private static boolean validV2Entry(JsonNode item, Instant now) {
        if(!item.isObject() || item.size()!=15
                || !item.path("entryId").isTextual() || !item.path("entryId").textValue().matches("[A-Za-z0-9_-]{1,64}")
                || !"shopping_preference".equals(item.path("memoryCategory").textValue())
                || !java.util.Set.of("phone","laptop","headphones").contains(item.path("productCategory").textValue())
                || !"self".equals(item.path("recipientScope").textValue())
                || !java.util.Set.of("explicit_user","user_confirmed").contains(item.path("source").textValue())
                || !"long_term_preference".equals(item.path("dataClass").textValue())
                || !item.path("semanticKey").isTextual() || !item.path("value").isTextual()
                || !ShoppingMemoryService.isControlledPreference("shopping_preference",item.path("semanticKey").textValue(),item.path("value").textValue())
                || !item.path("version").isInt() || item.path("version").intValue()<1
                || !"ACTIVE".equals(item.path("status").textValue()) || !item.path("chainVerified").booleanValue()
                || !item.path("createdAt").isTextual() || !item.path("updatedAt").isTextual() || !item.path("expiresAt").isTextual()) return false;
        JsonNode supersedes=item.get("supersedes");
        if(supersedes==null || !(supersedes.isNull() || supersedes.isTextual())) return false;
        try {
            Instant created=Instant.parse(item.path("createdAt").textValue());
            Instant updated=Instant.parse(item.path("updatedAt").textValue());
            Instant expires=Instant.parse(item.path("expiresAt").textValue());
            return item.path("createdAt").textValue().endsWith("Z")
                    && item.path("updatedAt").textValue().endsWith("Z")
                    && item.path("expiresAt").textValue().endsWith("Z")
                    && !updated.isBefore(created) && expires.isAfter(now) && expires.isAfter(updated)
                    && (item.path("version").intValue()==1 ? supersedes.isNull() : supersedes.isTextual());
        } catch(Exception invalid) { return false; }
    }
    private static String key(String owner) { try{return "memory:projection:v1:"+HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(owner.getBytes(StandardCharsets.UTF_8)));}catch(Exception e){throw new IllegalStateException(e);} }
    private static String keyV2(String owner) { try{return "memory:projection:v2:"+HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(owner.getBytes(StandardCharsets.UTF_8)));}catch(Exception e){throw new IllegalStateException(e);} }
}
