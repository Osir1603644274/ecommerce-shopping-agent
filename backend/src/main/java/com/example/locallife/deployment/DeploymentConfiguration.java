package com.example.locallife.deployment;

import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Configuration;

@Configuration
@EnableConfigurationProperties(DeploymentProperties.class)
class DeploymentConfiguration {
}
