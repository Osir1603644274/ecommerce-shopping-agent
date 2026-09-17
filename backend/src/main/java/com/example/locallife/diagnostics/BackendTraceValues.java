package com.example.locallife.diagnostics;

import java.lang.reflect.Method;
import java.util.*;

/** Business-field allowlist. Never stringify unknown objects, auth principals or exceptions. */
final class BackendTraceValues {
    private static final Set<String> FIELDS=Set.of("id","orderId","itemId","productId","campaignId","templateId",
        "itemType","quantity","status","currency","amountMinor","payableMinor","discountMinor","totalMinor",
        "unitPriceMinor","subtotalMinor","expectedPayableMinor","expectedUnitPriceMinor","pageSize","limit",
        "items","availableStock","totalStock","version","category","count","thresholdMinor","expectedVersion");
    static Object value(String name,Object value,int depth) {
        if(!FIELDS.contains(name)) return "[未公开字段]";
        if(value==null || value instanceof Number || value instanceof Boolean) return value;
        if(depth>3) return "[深度已截断]";
        if(value instanceof String s) return s.matches("[A-Za-z0-9_:/.-]{1,100}")?s:"[文本未公开]";
        if(value instanceof Enum<?> e) return e.name();
        if(value instanceof Collection<?> c) return c.stream().limit(20).map(v->summary(v,depth+1)).toList();
        return summary(value,depth+1);
    }
    static Object summary(Object value,int depth) {
        if(value==null || value instanceof Number || value instanceof Boolean) return value;
        if(depth>3) return "[深度已截断]";
        if(value instanceof Collection<?> c) return Map.of("count",c.size(),"items",c.stream().limit(10).map(v->summary(v,depth+1)).toList());
        if(!value.getClass().isRecord()) return Map.of("type",value.getClass().getSimpleName());
        var result=new LinkedHashMap<String,Object>();
        for(var field:value.getClass().getRecordComponents()) if(FIELDS.contains(field.getName())) {
            try { result.put(field.getName(),value(field.getName(),field.getAccessor().invoke(value),depth+1)); }
            catch(ReflectiveOperationException ignored) { result.put(field.getName(),"[不可读取]"); }
        }
        return result;
    }
    static Map<String,Object> arguments(Method method,Object[] values) {
        var result=new LinkedHashMap<String,Object>(); var names=method.getParameters();
        for(int i=0;i<Math.min(names.length,values.length);i++) {
            if(FIELDS.contains(names[i].getName())) result.put(names[i].getName(),value(names[i].getName(),values[i],0));
            else if(values[i]!=null && values[i].getClass().isRecord()) result.put(names[i].getName(),summary(values[i],0));
        }
        return result;
    }
    private BackendTraceValues() {}
}
