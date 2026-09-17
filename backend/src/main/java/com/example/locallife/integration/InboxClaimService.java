package com.example.locallife.integration;

import com.example.locallife.common.BusinessConflictException;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Propagation;
import org.springframework.transaction.annotation.Transactional;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Clock;
import java.time.LocalDateTime;
import java.util.HexFormat;

@Service
class InboxClaimService {
    private final InboxMapper mapper;
    private final MessagingProperties properties;
    private final Clock clock;

    @Autowired
    InboxClaimService(InboxMapper mapper, MessagingProperties properties) {
        this(mapper, properties, Clock.systemUTC());
    }

    InboxClaimService(
            InboxMapper mapper,
            MessagingProperties properties,
            Clock clock
    ) {
        this.mapper = mapper;
        this.properties = properties;
        this.clock = clock;
    }

    @Transactional(propagation = Propagation.REQUIRES_NEW)
    boolean claim(String consumer, EventEnvelope event) {
        String hash = sha256(event.payloadJson());
        LocalDateTime now = LocalDateTime.now(clock);
        try {
            mapper.insertClaim(consumer, event, hash, now);
            return true;
        } catch (DuplicateKeyException duplicate) {
            InboxEvent existing = mapper.find(consumer, event.id());
            if (existing == null) {
                throw duplicate;
            }
            if (!hash.equals(existing.payloadHash())
                    || !event.eventType().equals(existing.eventType())) {
                throw new BusinessConflictException("相同消息 ID 的事件类型或负载不一致");
            }
            if ("PROCESSED".equals(existing.status()) || "DEAD".equals(existing.status())) {
                return false;
            }
            boolean acquired = mapper.reclaim(
                    consumer,
                    event.id(),
                    now,
                    now.minus(properties.staleClaimAfter())
            ) == 1;
            if (!acquired) {
                // A live claim is not a receipt. Returning success here would let the
                // broker commit a message whose original executor may have crashed.
                throw new InboxBusyException(consumer, event.id());
            }
            return true;
        }
    }

    @Transactional(propagation = Propagation.REQUIRES_NEW)
    void processed(String consumer, String eventId) {
        if (mapper.markProcessed(consumer, eventId, LocalDateTime.now(clock)) != 1) {
            throw new IllegalStateException("Inbox 完成确认失败: " + eventId);
        }
    }

    @Transactional(propagation = Propagation.REQUIRES_NEW)
    boolean failed(String consumer, EventEnvelope event, Throwable failure) {
        String message = failure.getMessage() == null
                ? failure.getClass().getSimpleName()
                : failure.getMessage();
        if (message.length() > 1000) {
            message = message.substring(0, 1000);
        }
        mapper.markFailed(consumer, event.id(), message);
        InboxEvent failed = mapper.find(consumer, event.id());
        if (failed == null || failed.attempts() < properties.maxAttempts()) {
            return false;
        }
        mapper.insertDeadLetter(deadLetterSource(consumer), event, failed.attempts(), message);
        if (mapper.markDead(consumer, event.id()) != 1) {
            throw new IllegalStateException("Inbox 死信状态更新失败: " + event.id());
        }
        return true;
    }

    private static String deadLetterSource(String consumer) {
        String source = "INBOX:" + consumer;
        return source.length() <= 64 ? source : source.substring(0, 64);
    }

    private static String sha256(String payload) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                    .digest(payload.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception exception) {
            throw new IllegalStateException("无法生成 Inbox 负载摘要", exception);
        }
    }
}
