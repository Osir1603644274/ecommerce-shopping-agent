package com.example.locallife.payment;

import org.springframework.boot.context.properties.ConfigurationProperties;

import java.time.Duration;

@ConfigurationProperties("local-life.payment")
public record PaymentProperties(
        String callbackSecret,
        Duration callbackTolerance,
        boolean simulatorEnabled
) {
    public PaymentProperties {
        callbackTolerance = callbackTolerance == null ? Duration.ofMinutes(5) : callbackTolerance;
    }
}
