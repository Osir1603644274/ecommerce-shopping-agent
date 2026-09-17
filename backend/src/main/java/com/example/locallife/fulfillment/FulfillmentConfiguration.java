package com.example.locallife.fulfillment;

import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Configuration;

@Configuration
@EnableConfigurationProperties(FulfillmentProperties.class)
class FulfillmentConfiguration { }
