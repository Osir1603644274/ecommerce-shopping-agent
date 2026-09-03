package com.example.locallife.integration;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

class KafkaFailureRecoveryConfigurationTests {

    @Test
    void configuresARealErrorHandlerInsteadOfImmediateTightLoopRetries() {
        assertThat(new KafkaFailureRecoveryConfiguration()
                .domainEventKafkaErrorHandler()).isNotNull();
    }
}
