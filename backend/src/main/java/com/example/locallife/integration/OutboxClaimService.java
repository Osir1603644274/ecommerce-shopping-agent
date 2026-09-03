package com.example.locallife.integration;

import org.springframework.stereotype.Service;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.transaction.annotation.Propagation;
import org.springframework.transaction.annotation.Transactional;

import java.time.Clock;
import java.time.LocalDateTime;
import java.util.List;
import java.util.Optional;

@Service
class OutboxClaimService {
    private final OutboxMapper mapper;
    private final MessagingProperties properties;
    private final Clock clock;

    @Autowired
    OutboxClaimService(OutboxMapper mapper, MessagingProperties properties) {
        this(mapper, properties, Clock.systemUTC());
    }

    OutboxClaimService(
            OutboxMapper mapper,
            MessagingProperties properties,
            Clock clock
    ) {
        this.mapper = mapper;
        this.properties = properties;
        this.clock = clock;
    }

    List<String> dispatchableIds(int limit) {
        LocalDateTime now = LocalDateTime.now(clock);
        return mapper.findDispatchableIds(
                now,
                now.minus(properties.staleClaimAfter()),
                Math.max(1, Math.min(limit, 500))
        );
    }

    @Transactional(propagation = Propagation.REQUIRES_NEW)
    Optional<OutboxEvent> claim(String id, String owner) {
        LocalDateTime now = LocalDateTime.now(clock);
        if (mapper.claim(id, owner, now, now.minus(properties.staleClaimAfter())) != 1) {
            return Optional.empty();
        }
        return Optional.ofNullable(mapper.findById(id));
    }

    @Transactional(propagation = Propagation.REQUIRES_NEW)
    void published(String id, String owner) {
        if (mapper.markPublished(id, owner, LocalDateTime.now(clock)) != 1) {
            throw new IllegalStateException("Outbox 发布确认失去所有权: " + id);
        }
    }

    @Transactional(propagation = Propagation.REQUIRES_NEW)
    void failed(OutboxEvent event, String owner, Throwable failure) {
        int nextAttempt = event.attempts() + 1;
        String error = truncate(failure.getMessage() == null
                ? failure.getClass().getSimpleName()
                : failure.getMessage(), 1000);
        boolean dead = nextAttempt >= properties.maxAttempts();
        if (dead) {
            mapper.insertDeadLetter(event, nextAttempt, error);
        }
        long backoffSeconds = Math.min(300L, 1L << Math.min(nextAttempt, 8));
        int updated = mapper.markFailure(
                event.id(),
                owner,
                dead ? "DEAD" : "PENDING",
                LocalDateTime.now(clock).plusSeconds(backoffSeconds),
                error
        );
        if (updated != 1) {
            throw new IllegalStateException("Outbox 失败确认失去所有权: " + event.id());
        }
    }

    private static String truncate(String value, int maxLength) {
        return value.length() <= maxLength ? value : value.substring(0, maxLength);
    }
}
