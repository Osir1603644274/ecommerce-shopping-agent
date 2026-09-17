package com.example.locallife.diagnostics;

import com.github.benmanes.caffeine.cache.*;
import java.time.Duration;
import org.springframework.stereotype.Component;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;

@Component
@ConditionalOnProperty(name="local-life.observer.enabled",havingValue="true")
public class BackendTraceStore {
    private final Cache<String,BackendTrace.Context> traces=Caffeine.newBuilder().maximumSize(500).expireAfterWrite(Duration.ofMinutes(15)).build();
    public void save(BackendTrace.Context context) { traces.put(context.id,context); }
    public BackendTrace.Context get(String id) { return traces.getIfPresent(id); }
}
