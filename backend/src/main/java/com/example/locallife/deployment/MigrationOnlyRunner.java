package com.example.locallife.deployment;

import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.ConfigurableApplicationContext;
import org.springframework.stereotype.Component;

@Component
@ConditionalOnProperty(
        prefix = "local-life.deployment",
        name = "migration-only",
        havingValue = "true"
)
class MigrationOnlyRunner implements ApplicationRunner {
    private final ConfigurableApplicationContext context;

    MigrationOnlyRunner(ConfigurableApplicationContext context) {
        this.context = context;
    }

    @Override
    public void run(ApplicationArguments args) {
        int exitCode = SpringApplication.exit(context);
        if (exitCode != 0) {
            throw new IllegalStateException("数据库迁移进程异常退出: " + exitCode);
        }
    }
}
