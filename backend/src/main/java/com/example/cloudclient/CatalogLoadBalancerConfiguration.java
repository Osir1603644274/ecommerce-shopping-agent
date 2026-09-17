package com.example.cloudclient;

import org.springframework.cloud.loadbalancer.core.ServiceInstanceListSupplier;
import org.springframework.context.ConfigurableApplicationContext;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.web.client.RestTemplate;

/** Outside the application component scan: applies only to the catalog client context. */
@Configuration(proxyBeanMethods=false)
public class CatalogLoadBalancerConfiguration {
    @Bean
    ServiceInstanceListSupplier catalogInstances(ConfigurableApplicationContext context){
        var factory=new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(500);factory.setReadTimeout(500);
        return ServiceInstanceListSupplier.builder().withBlockingDiscoveryClient()
            .withBlockingHealthChecks(new RestTemplate(factory)).build(context);
    }
}
