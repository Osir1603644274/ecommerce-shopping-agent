package com.example.locallife.integration;

/** Retry the delivery until its durable claim expires; never acknowledge it as a duplicate. */
final class InboxBusyException extends RuntimeException {
    InboxBusyException(String consumer, String eventId) {
        super("Inbox claim is still active: " + consumer + "/" + eventId);
    }
}
