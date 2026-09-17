package com.example.locallife.diagnostics;

import org.aspectj.lang.ProceedingJoinPoint;
import org.aspectj.lang.annotation.*;
import org.springframework.stereotype.Component;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.security.core.context.SecurityContextHolder;

@Aspect @Component
@ConditionalOnProperty(name="local-life.observer.enabled",havingValue="true")
public class BackendTraceAspect {
    @Around("execution(public * com.example.locallife..*Service.*(..)) || execution(public * com.example.locallife..*Controller.*(..)) || execution(public * com.example.locallife..*RedisGateway.*(..)) || execution(public * com.example.locallife..*Cache.*(..))")
    public Object observe(ProceedingJoinPoint call) throws Throwable {
        if(BackendTrace.current()==null || call.getSignature().getDeclaringTypeName().contains(".diagnostics.")) return call.proceed();
        var auth=SecurityContextHolder.getContext().getAuthentication();
        if(call.getSignature().getDeclaringTypeName().endsWith("Controller")) {
            var event=BackendTrace.start("security","进入 Controller 时的身份状态");
            BackendTrace.finish(event,auth!=null && auth.isAuthenticated() && !"anonymousUser".equals(auth.getPrincipal())?"authenticated":"anonymous",null);
        }
        var event=BackendTrace.start("method",call.getSignature().getDeclaringTypeName()+"."+call.getSignature().getName());
        if(event!=null) try {
            var method=((org.aspectj.lang.reflect.MethodSignature)call.getSignature()).getMethod();
            event.input=BackendTraceValues.arguments(method,call.getArgs());
            event.source=BackendTraceSource.lookup(method.getDeclaringClass().getName(),method.getName());
        } catch(RuntimeException ignored) { /* Missing diagnostic metadata must not fail the call. */ }
        try { Object result=call.proceed();
            if(event!=null) try { event.output=BackendTraceValues.summary(result,0); } catch(RuntimeException ignored) {}
            BackendTrace.finish(event,"returned",null); return result; }
        catch(Throwable error) { BackendTrace.finish(event,"threw:"+error.getClass().getSimpleName(),null); throw error; }
    }
}
