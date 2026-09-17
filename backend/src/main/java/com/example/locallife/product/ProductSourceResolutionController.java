package com.example.locallife.product;

import com.example.locallife.common.ApiResponse;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotEmpty;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.*;
import java.util.ArrayList;
import java.util.List;
import java.util.HexFormat;

/** Public read-only resolution. A document is not authorization to create an order. */
@RestController
public class ProductSourceResolutionController {
    private final JdbcTemplate jdbc;
    public ProductSourceResolutionController(JdbcTemplate jdbc) { this.jdbc=jdbc; }

    @PostMapping("/api/products/resolve-sources")
    public ApiResponse<List<Resolved>> resolve(@Valid @RequestBody Request request) {
        var parameters=new ArrayList<Object>();
        var clauses=new ArrayList<String>();
        for (Identity identity:request.identities()) {
            clauses.add("(p.source=? AND p.source_item_id=? AND i.raw_sha=?)");
            parameters.add(identity.source());parameters.add(identity.sourceItemId());parameters.add(HexFormat.of().parseHex(identity.rawSha256()));
        }
        String sql="SELECT p.id,p.source,p.source_item_id,i.raw_sha "
            +"FROM product p JOIN external_catalog_identity i ON i.product_id=p.id "
            +"WHERE ("+String.join(" OR ",clauses)+") AND p.lifecycle_status='ACTIVE'";
        var rows=jdbc.query(sql,statement->{
            for(int i=0;i<parameters.size();i++) statement.setObject(i+1,parameters.get(i));
            CatalogReadBudget.apply(statement);
        },(rs,n)->new Resolved(
            Long.toString(rs.getLong("id")),rs.getString("source"),rs.getString("source_item_id"),HexFormat.of().formatHex(rs.getBytes("raw_sha"))));
        return ApiResponse.ok(rows);
    }
    public record Identity(
        @NotNull @Pattern(regexp="kuaisearch|multicpr") String source,
        @NotNull @Pattern(regexp="[1-9][0-9]{0,11}") String sourceItemId,
        @NotNull @Pattern(regexp="[a-f0-9]{64}") String rawSha256) { }
    public record Request(@NotEmpty @Size(max=20) List<@NotNull @Valid Identity> identities) { }
    public record Resolved(String productId,String source,String sourceItemId,String rawSha256) { }
}
