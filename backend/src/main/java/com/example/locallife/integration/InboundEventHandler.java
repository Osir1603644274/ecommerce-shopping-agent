package com.example.locallife.integration;

public interface InboundEventHandler {
    boolean supports(String eventType);

    void handle(EventEnvelope event);
}
