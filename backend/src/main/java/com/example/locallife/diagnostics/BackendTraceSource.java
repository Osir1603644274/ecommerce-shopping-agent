package com.example.locallife.diagnostics;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;
import java.util.regex.Pattern;

/** Source excerpts come from this build's packaged allowlisted source files, not host disk. */
final class BackendTraceSource {
    static Map<String,Object> lookup(String className,String method) {
        if(!className.startsWith("com.example.locallife.")) return Map.of();
        String file=className.split("\\$")[0].replace('.','/')+".java";
        try(var stream=BackendTraceSource.class.getResourceAsStream("/observer-source/"+file)) {
            if(stream==null) return Map.of("symbol",className+"."+method,"notice","此构建未附此方法源码");
            byte[] raw=stream.readNBytes(150000); String source=new String(raw,StandardCharsets.UTF_8);
            var result=new LinkedHashMap<String,Object>();
            result.put("file","backend/src/main/java/"+file); result.put("symbol",className+"."+method);
            result.put("sha256",HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(raw)));
            var pattern=Pattern.compile("^\\s*(?:(?:public|private|protected|static|final|synchronized|abstract)\\s+)*[\\w<>\\[\\], ?]+\\s+"+Pattern.quote(method)+"\\s*\\(");
            String[] lines=source.split("\\R"); var hits=new ArrayList<Integer>();
            for(int i=0;i<lines.length;i++) if(pattern.matcher(lines[i]).find()) hits.add(i);
            if(hits.size()==1) {
                int declaration=hits.get(0), first=declaration,last=Math.min(lines.length,declaration+40);
                // Include the preceding mapper annotation, so SQL code is not lost.
                while(first>0 && declaration-first<45 && !lines[first-1].isBlank()
                        && !lines[first-1].stripTrailing().endsWith(";") && !lines[first-1].strip().equals("}")) first--;
                // A one-line interface declaration ends here, not in the next mapper method.
                if(lines[declaration].stripTrailing().endsWith(";")) last=declaration+1;
                var snippet=new StringBuilder();
                for(int i=first;i<last;i++) snippet.append(i+1).append(": ").append(lines[i]).append('\n');
                result.put("line",first+1); result.put("snippet",snippet.toString());
            } else result.put("notice","重载或多行声明未唯一定位，仅展示真实调用符号");
            return result;
        } catch(Exception ignored) { return Map.of(); }
    }
    private BackendTraceSource() {}
}
