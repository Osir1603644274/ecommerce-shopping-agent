package com.example.locallife.deployment;

import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Profile;
import org.springframework.cloud.loadbalancer.annotation.LoadBalancerClient;
import com.example.cloudclient.CatalogLoadBalancerConfiguration;

/** Blocking Feign health-check discovery requires a non-load-balanced HTTP client. */
@Configuration(proxyBeanMethods=false)
@Profile("micro-trade")
@LoadBalancerClient(name="commerce-catalog",configuration=CatalogLoadBalancerConfiguration.class)
public class CatalogDiscoveryConfiguration { }
