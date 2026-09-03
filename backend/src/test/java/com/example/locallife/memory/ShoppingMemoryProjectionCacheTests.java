package com.example.locallife.memory;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.SerializationFeature;
import org.junit.jupiter.api.Test;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.ValueOperations;
import java.lang.reflect.Method;
import java.time.Instant;
import java.time.Duration;
import java.time.Clock;
import java.time.ZoneOffset;
import java.util.concurrent.atomic.AtomicReference;
import java.util.List;
import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class ShoppingMemoryProjectionCacheTests {
    private final StringRedisTemplate redis=mock(StringRedisTemplate.class);
    private final ValueOperations<String,String> values=mock(ValueOperations.class);
    private final ObjectMapper json=new ObjectMapper().findAndRegisterModules().disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS);
    private final ShoppingMemoryProjectionCache cache=new ShoppingMemoryProjectionCache(redis,json);
    private final ShoppingMemoryService.MemoryResponse entry=new ShoppingMemoryService.MemoryResponse("id","shopping_preference","avoid_brand","brand-x",1,"ACTIVE");
    private String envelope(long revision, String expires) throws Exception { return json.writeValueAsString(java.util.Map.of("schemaVersion",1,"revision",revision,"entries",List.of(entry),"createdAt",Instant.now().toString(),"expiresAt",expires)); }
    @Test void keyIsHashedAndDoesNotContainOwner() throws Exception { Method m=ShoppingMemoryProjectionCache.class.getDeclaredMethod("key",String.class);m.setAccessible(true);String key=(String)m.invoke(null,"owner-secret");assertThat(key).doesNotContain("owner-secret").startsWith("memory:projection:v1:"); }
    @Test void validRevisionHits() throws Exception { when(redis.opsForValue()).thenReturn(values);when(values.get(anyString())).thenReturn(envelope(2,Instant.now().plusSeconds(30).toString()));assertThat(cache.get("owner",2)).containsExactly(entry); }
    @Test void staleRevisionMisses() throws Exception { when(redis.opsForValue()).thenReturn(values);when(values.get(anyString())).thenReturn(envelope(1,Instant.now().plusSeconds(30).toString()));assertThat(cache.get("owner",2)).isNull(); }
    @Test void corruptMisses() { when(redis.opsForValue()).thenReturn(values);when(values.get(anyString())).thenReturn("{");assertThat(cache.get("owner",1)).isNull(); }
    @Test void expiredMisses() throws Exception { when(redis.opsForValue()).thenReturn(values);when(values.get(anyString())).thenReturn(envelope(1,Instant.now().minusSeconds(1).toString()));assertThat(cache.get("owner",1)).isNull(); }
    @Test void oversizedMisses() throws Exception { var huge=new ShoppingMemoryService.MemoryResponse("x".repeat(600),"shopping_preference","avoid_brand","brand-x",1,"ACTIVE");String raw=json.writeValueAsString(java.util.Map.of("schemaVersion",1,"revision",1,"entries",List.of(huge),"createdAt",Instant.now().toString(),"expiresAt",Instant.now().plusSeconds(30).toString()));when(redis.opsForValue()).thenReturn(values);when(values.get(anyString())).thenReturn(raw);assertThat(cache.get("owner",1)).isNull(); }
    @Test void redisGetExceptionMisses() { when(redis.opsForValue()).thenThrow(new RuntimeException());assertThat(cache.get("owner",1)).isNull(); }
    @Test void redisSetExceptionIsTolerated() { when(redis.opsForValue()).thenThrow(new RuntimeException());cache.put("owner",1,List.of(entry)); }
    @Test void redisDeleteExceptionIsTolerated() { doThrow(new RuntimeException()).when(redis).delete(anyString());cache.evict("owner"); }
    @Test void producedEnvelopeRoundTripsWithinExactTtl() throws Exception {
        AtomicReference<String> stored=new AtomicReference<>();
        when(redis.opsForValue()).thenReturn(values);
        doAnswer(call -> { stored.set(call.getArgument(1)); return null; }).when(values).set(anyString(),anyString(),any());
        cache.put("owner",1,List.of(entry));
        var root=json.readTree(stored.get());
        assertThat(Duration.between(Instant.parse(root.get("createdAt").textValue()),Instant.parse(root.get("expiresAt").textValue()))).isEqualTo(Duration.ofSeconds(60));
        when(values.get(anyString())).thenAnswer(call -> stored.get());
        assertThat(cache.get("owner",1)).containsExactly(entry);
    }
    @Test void strictScalarAndTimestampVariantsMiss() throws Exception {
        when(redis.opsForValue()).thenReturn(values);
        for (String raw : List.of(
                envelope(1,Instant.now().plusSeconds(30).toString()).replace("\"schemaVersion\":1","\"schemaVersion\":1.0"),
                envelope(1,Instant.now().plusSeconds(30).toString()).replace("\"revision\":1","\"revision\":-1"),
                envelope(1,Instant.now().plusSeconds(30).toString()).replace("\"createdAt\":\"","\"createdAt\":\"not-a-date"),
                envelope(1,"2099-01-01T00:00:00Z"),
                envelope(1,Instant.now().plusSeconds(120).toString()).replace("\"createdAt\":\"","\"createdAt\":\"2026-01-01T00:00:00Z"))) {
            when(values.get(anyString())).thenReturn(raw); assertThat(cache.get("owner",1)).isNull();
        }
    }
    @Test void unknownRootAndMalformedEntriesMiss() throws Exception {
        when(redis.opsForValue()).thenReturn(values);
        String valid=envelope(1,Instant.now().plusSeconds(30).toString());
        for(String raw:List.of(valid.substring(0,valid.length()-1)+",\"owner\":\"x\"}",valid.replace("[{","[{}"),valid.replace("\"brand-x\"","\"unknownsecret\""))){when(values.get(anyString())).thenReturn(raw);assertThat(cache.get("owner",1)).isNull();}
    }
    @Test void duplicateKeysAndFutureOrEqualTimesMiss() throws Exception {
        when(redis.opsForValue()).thenReturn(values);
        String valid=envelope(1,Instant.now().plusSeconds(30).toString());
        String duplicate=valid.replace("\"revision\":1", "\"revision\":1,\"revision\":1");
        String future=valid.replaceFirst("\"createdAt\":\"[^\"]+", "\"createdAt\":\"2099-01-01T00:00:00Z");
        for(String raw:List.of(duplicate,future)){when(values.get(anyString())).thenReturn(raw);assertThat(cache.get("owner",1)).isNull();}
    }

    @Test void v2CacheIsRevisionBoundAndExpiresAtEarliestEntry() throws Exception {
        Instant now=Instant.parse("2026-08-29T00:00:00Z");
        ShoppingMemoryProjectionCache v2=new ShoppingMemoryProjectionCache(redis,json,Clock.fixed(now,ZoneOffset.UTC));
        AtomicReference<String> stored=new AtomicReference<>();
        when(redis.opsForValue()).thenReturn(values);
        doAnswer(call -> { stored.set(call.getArgument(1)); return null; }).when(values).set(anyString(),anyString(),any());
        MemoryProjectionV2Response.Entry item=new MemoryProjectionV2Response.Entry(
                "entry-v2","shopping_preference","phone","self","os","android","explicit_user",
                "long_term_preference",1,"ACTIVE",now.minusSeconds(20),now.minusSeconds(10),now.plusSeconds(25),null,true
        );
        MemoryProjectionV2Response response=new MemoryProjectionV2Response(2,7,MemoryProjectionV2Response.bindOwner("owner"),true,List.of(item));
        v2.putV2("owner",response);
        JsonNode root=json.readTree(stored.get());
        assertThat(root.size()).isEqualTo(7);
        assertThat(root.path("entries").get(0).size()).isEqualTo(15);
        Method valid=ShoppingMemoryProjectionCache.class.getDeclaredMethod("validV2Entry",JsonNode.class,Instant.class);
        valid.setAccessible(true);
        assertThat(valid.invoke(null,root.path("entries").get(0),now)).isEqualTo(true);
        assertThat(root.path("expiresAt").textValue()).isEqualTo(now.plusSeconds(25).toString());
        when(values.get(anyString())).thenReturn(stored.get());
        assertThat(v2.getV2("owner",8)).isNull();
        assertThat(v2.getV2("other-owner",7)).isNull();
        assertThat(v2.getV2("owner",7)).isEqualTo(response);
    }
}
