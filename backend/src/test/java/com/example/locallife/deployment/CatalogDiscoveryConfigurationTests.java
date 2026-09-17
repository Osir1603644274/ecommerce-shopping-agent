package com.example.locallife.deployment;

import org.junit.jupiter.api.Test;
import org.springframework.cloud.client.discovery.DiscoveryClient;
import org.springframework.cloud.client.DefaultServiceInstance;
import com.example.cloudclient.CatalogLoadBalancerConfiguration;
import org.springframework.cloud.loadbalancer.core.ServiceInstanceListSupplier;
import org.springframework.cloud.loadbalancer.support.LoadBalancerClientFactory;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;
import static org.assertj.core.api.Assertions.assertThat;

class CatalogDiscoveryConfigurationTests {
    @Test void blockingHealthSupplierHasARealClientAndConfiguredInstances(){
        new ApplicationContextRunner()
            .withUserConfiguration(CatalogLoadBalancerConfiguration.class)
            .withBean(DiscoveryClient.class,()->new DiscoveryClient(){
                public String description(){return "isolated test";}
                public java.util.List<String> getServices(){return java.util.List.of("commerce-catalog");}
                public java.util.List<org.springframework.cloud.client.ServiceInstance> getInstances(String id){return java.util.List.of(new DefaultServiceInstance("catalog-a",id,"127.0.0.1",1,false));}
            })
            .withBean(LoadBalancerClientFactory.class,()->new LoadBalancerClientFactory(new org.springframework.cloud.client.loadbalancer.LoadBalancerClientsProperties()))
            .withPropertyValues("spring.profiles.active=micro-trade","spring.cloud.discovery.reactive.enabled=false",
                "loadbalancer.client.name=commerce-catalog","spring.cloud.loadbalancer.configurations=health-check",
                "spring.cloud.discovery.client.simple.instances.commerce-catalog[0].uri=http://127.0.0.1:1")
            .run(context->{
                assertThat(context).hasNotFailed();
                assertThat(context).hasSingleBean(ServiceInstanceListSupplier.class);
                assertThat(context.getBean(org.springframework.cloud.client.discovery.DiscoveryClient.class).getInstances("commerce-catalog")).hasSize(1);
            });
    }
}
