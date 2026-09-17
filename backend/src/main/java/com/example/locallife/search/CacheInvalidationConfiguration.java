package com.example.locallife.search;

import org.springframework.amqp.core.*;
import org.springframework.amqp.rabbit.connection.ConnectionFactory;
import org.springframework.amqp.rabbit.listener.SimpleMessageListenerContainer;
import org.springframework.boot.autoconfigure.condition.ConditionalOnExpression;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
@ConditionalOnExpression("${local-life.cache.shop.enabled:true} or ${local-life.cache.product.enabled:true}")
class CacheInvalidationConfiguration {
    @Bean FanoutExchange cacheInvalidationExchange() { return new FanoutExchange(CacheInvalidationPublisher.CHANNEL,true,false); }
    @Bean Queue cacheInvalidationQueue() { return new AnonymousQueue(); }
    @Bean Binding cacheInvalidationBinding(Queue cacheInvalidationQueue, FanoutExchange cacheInvalidationExchange) {
        return BindingBuilder.bind(cacheInvalidationQueue).to(cacheInvalidationExchange);
    }
    @Bean SimpleMessageListenerContainer cacheInvalidationListenerContainer(ConnectionFactory connectionFactory,
            Queue cacheInvalidationQueue,CacheInvalidationSubscriber subscriber) {
        var container=new SimpleMessageListenerContainer(connectionFactory);
        container.setQueues(cacheInvalidationQueue);
        container.setAcknowledgeMode(AcknowledgeMode.AUTO);
        container.setPrefetchCount(20); container.setMissingQueuesFatal(false); container.setRecoveryInterval(1000);
        container.setMessageListener((MessageListener)message->subscriber.accept(new String(message.getBody(),java.nio.charset.StandardCharsets.UTF_8)));
        return container;
    }
}
