package com.example.locallife.product;

import java.sql.Statement;
import org.apache.ibatis.executor.statement.StatementHandler;
import org.apache.ibatis.plugin.*;
import org.apache.ibatis.session.ResultHandler;
import org.springframework.stereotype.Component;

/** Apply immediately before execution, after MyBatis/transaction timeout settings. */
@Component
@Intercepts(@Signature(type = StatementHandler.class, method = "query",
        args = {Statement.class, ResultHandler.class}))
public class CatalogSqlTimeoutInterceptor implements Interceptor {
    @Override public Object intercept(Invocation invocation) throws Throwable {
        CatalogReadBudget.apply((Statement) invocation.getArgs()[0]);
        return invocation.proceed();
    }
}
