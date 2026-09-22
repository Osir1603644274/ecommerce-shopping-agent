package com.example.locallife.support;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;

/** Explicit merchant/simulator declarations only; never infer a SKU from free-form titles. */
@Component
public class SaleSpecificationRegistry {
    private final JdbcTemplate jdbc;
    private final ObjectMapper json;
    private final boolean enabled;
    public SaleSpecificationRegistry(JdbcTemplate jdbc,ObjectMapper json,
            @Value("${local-life.support.enabled:false}") boolean enabled) {
        this.jdbc=jdbc;this.json=json;this.enabled=enabled;
    }
    public String enrich(long productId,String evidence) {
        if(!enabled) return evidence;
        try {
            ObjectNode snapshot=(ObjectNode)json.readTree(evidence);
            snapshot.put("supportPolicyVersion",AfterSalePolicy.VERSION);
            var specs=jdbc.query("SELECT code,label,version FROM support_sale_specification WHERE product_id=? AND enabled=TRUE",
                    (rs,n)->new Spec(rs.getString(1),rs.getString(2),rs.getLong(3)),productId);
            if(!specs.isEmpty()) {
                Spec spec=specs.get(0);
                if(spec.code()==null || spec.code().isBlank() || spec.label()==null || spec.label().isBlank() || spec.version()<1)
                    throw new IllegalStateException("销售规格声明损坏");
                ObjectNode value=snapshot.putObject("saleSpecification");
                value.put("code",spec.code());value.put("label",spec.label());value.put("version",spec.version());
            }
            return json.writeValueAsString(snapshot);
        } catch(java.io.IOException exception) { throw new IllegalStateException("购买规格快照写入失败",exception); }
    }
    private record Spec(String code,String label,long version) { }
}
