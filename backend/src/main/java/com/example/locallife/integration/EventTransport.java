package com.example.locallife.integration;

interface EventTransport {
    void publish(EventEnvelope event);

    String name();
}
