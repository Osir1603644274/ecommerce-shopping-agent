package com.example.locallife.diagnostics;

import java.sql.Statement;
import java.util.List;
import org.apache.ibatis.executor.statement.StatementHandler;
import org.apache.ibatis.mapping.MappedStatement;
import org.apache.ibatis.plugin.*;
import org.apache.ibatis.reflection.SystemMetaObject;
import org.apache.ibatis.session.ResultHandler;
import org.springframework.stereotype.Component;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;

@Component
@ConditionalOnProperty(name="local-life.observer.enabled",havingValue="true")
@Intercepts({@Signature(type=StatementHandler.class,method="query",args={Statement.class,ResultHandler.class}),
             @Signature(type=StatementHandler.class,method="update",args={Statement.class}),
             @Signature(type=StatementHandler.class,method="batch",args={Statement.class})})
public class BackendSqlObserver implements Interceptor {
    public Object intercept(Invocation invocation) throws Throwable {
        if(BackendTrace.current()==null) return invocation.proceed();
        String id="MyBatis."+invocation.getMethod().getName();
        MappedStatement statement=null;
        try {
            var meta=SystemMetaObject.forObject(invocation.getTarget());
            if(meta.hasGetter("delegate.mappedStatement")) {
                var mapped=(MappedStatement)meta.getValue("delegate.mappedStatement");
                statement=mapped;
                id=mapped.getId()+" ["+mapped.getSqlCommandType()+"]";
            }
        } catch(RuntimeException ignored) { /* Unknown plugin wrapper: keep the generic label, not a business failure. */ }
        BackendTrace.observeTransaction();
        var event=BackendTrace.start("sql",id);
        if(event!=null && statement!=null) try {
            int dot=statement.getId().lastIndexOf('.');
            if(dot>0) event.source=BackendTraceSource.lookup(statement.getId().substring(0,dot),statement.getId().substring(dot+1));
            var bound=((StatementHandler)invocation.getTarget()).getBoundSql();
            // Parameter names and values are separate. Never interpolate literals into SQL.
            var parameters=new java.util.LinkedHashMap<String,Object>();
            Object target=bound.getParameterObject();
            for(var mapping:bound.getParameterMappings()) {
                if(parameters.size()>=50) break;
                String property=mapping.getProperty(), name=property.substring(property.lastIndexOf('.')+1);
                Object value=null;
                if(bound.hasAdditionalParameter(property)) value=bound.getAdditionalParameter(property);
                else if(target!=null && statement.getConfiguration().getTypeHandlerRegistry().hasTypeHandler(target.getClass())) value=target;
                else if(target!=null) value=statement.getConfiguration().newMetaObject(target).getValue(property);
                parameters.put(property,BackendTraceValues.value(name,value,0));
            }
            // Hide string/numeric literals, including SQL generated with ${...}.
            String template=bound.getSql().replaceAll("(?s)/\\*.*?\\*/", " ").replaceAll("(?m)--[^\\r\\n]*|#[^\\r\\n]*", " ")
                .replaceAll("'([^']|'')*'|\"([^\"]|\"\")*\"","?").replaceAll("\\b\\d+(?:\\.\\d+)?\\b","?").replaceAll("\\s+"," ").trim();
            event.input=java.util.Map.of("sqlTemplate",template.substring(0,Math.min(2500,template.length())),"bindings",parameters);
        } catch(RuntimeException ignored) { /* Unknown plugins retain generic event. */ }
        try {
            var result=invocation.proceed();
            BackendTrace.finish(event,invocation.getMethod().getName().equals("batch")?"queued_batch":"executed",
                result instanceof Integer n?n:result instanceof List<?> list?list.size():null);
            return result;
        } catch(Throwable error) { BackendTrace.finish(event,"failed",null); throw error; }
    }
}
