package com.example.locallife.ordering;

import feign.Retryer;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
class CloudFeignConfiguration {
    /** The current read-only catalog Feign client does not retry automatically. */
    @Bean
    Retryer neverRetryFeignRequests() {
        return Retryer.NEVER_RETRY;
    }
}
