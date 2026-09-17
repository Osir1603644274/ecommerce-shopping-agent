package com.example.locallife.inventory.remote;

import com.example.locallife.common.ResourceNotFoundException;
import com.example.locallife.inventory.InventoryStock;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;
import java.net.URI;
import java.net.http.*;
import java.time.Duration;

@Component
@ConditionalOnProperty(name="local-life.inventory.remote-enabled",havingValue="true")
public class RemoteInventoryClient implements com.example.locallife.inventory.InventoryReadPort {
    private final ObjectMapper json;
    private final String url,token;
    private final HttpClient client=HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(1)).followRedirects(HttpClient.Redirect.NEVER).build();
    public RemoteInventoryClient(ObjectMapper json,@Value("${local-life.inventory.service-url}") String url,
                                 @Value("${local-life.inventory.service-token}") String token){
        this.json=json;this.url=url.replaceAll("/+$","");this.token=token;
        URI uri=URI.create(url);
        if(token.length()<32 || !java.util.Set.of("http","https").contains(uri.getScheme()) || uri.getHost()==null || uri.getUserInfo()!=null)
            throw new IllegalArgumentException("invalid_inventory_service_configuration");
    }
    private HttpResponse<String> request(String method,String path,Object body){
        try{
            var builder=HttpRequest.newBuilder(URI.create(url+"/internal/inventory"+path))
                .timeout(Duration.ofSeconds(2)).header("X-Inventory-Service-Token",token).header("Content-Type","application/json");
            if("GET".equals(method))builder.GET();else builder.POST(HttpRequest.BodyPublishers.ofString(json.writeValueAsString(body)));
            return client.send(builder.build(),HttpResponse.BodyHandlers.ofString());
        }catch(InterruptedException interrupted){Thread.currentThread().interrupt();throw new IllegalStateException("inventory_request_interrupted",interrupted);}
        catch(Exception unavailable){throw new IllegalStateException("inventory_service_unavailable",unavailable);}
    }
    public InventoryStock stock(String type,Long id){
        if(!java.util.Set.of("PRODUCT","LOCAL_DEAL").contains(type) || id==null || id<=0)throw new IllegalArgumentException("invalid_stock_identity");
        var response=request("GET","/stocks/"+type+"/"+id,null);
        if(response.statusCode()==404)throw new ResourceNotFoundException("商品库存未配置");
        if(response.statusCode()!=200)throw new IllegalStateException("inventory_read_unavailable");
        try{
            InventoryStock stock=json.readValue(response.body(),InventoryStock.class);
            if(!id.equals(stock.itemId()) || !type.equals(stock.itemType()))throw new IllegalStateException("stock_identity_mismatch");
            return stock;
        }catch(Exception invalid){throw new IllegalStateException("invalid_inventory_response",invalid);}
    }
    public java.util.List<InventoryStock> stocks(String type,java.util.List<Long> ids){
        if(ids.isEmpty())return java.util.List.of();
        if(ids.size()>500 || ids.stream().anyMatch(id->id==null || id<=0))throw new IllegalArgumentException("invalid_stock_batch");
        var response=request("POST","/stocks/query",java.util.Map.of("itemType",type,"itemIds",ids));
        if(response.statusCode()!=200)throw new IllegalStateException("inventory_read_unavailable");
        try{
            var stocks=json.readValue(response.body(),new com.fasterxml.jackson.core.type.TypeReference<java.util.List<InventoryStock>>(){});
            var seen=new java.util.HashSet<Long>();
            for(var stock:stocks)if(!type.equals(stock.itemType()) || !ids.contains(stock.itemId()) || !seen.add(stock.itemId()))throw new IllegalStateException("stock_batch_identity_mismatch");
            return stocks;
        }catch(Exception invalid){throw new IllegalStateException("invalid_inventory_response",invalid);}
    }
    public InventoryStock createStock(String type,Long id,int quantity){
        var response=request("POST","/stocks",new StockCommand.Item(type,id,quantity));
        if(response.statusCode()==409)throw new com.example.locallife.common.BusinessConflictException("库存已配置且数量不同");
        if(response.statusCode()!=200)throw new IllegalStateException("inventory_creation_unresolved");
        return stock(type,id);
    }
    public JsonNode apply(StockCommand command){
        HttpResponse<String> response;
        try{response=request("POST","/commands",command);}
        catch(IllegalStateException unknown){
            // Never create a new key on ambiguous completion; read the durable receipt.
            response=request("GET","/commands/"+command.commandId(),null);
        }
        if(response.statusCode()!=200)throw new IllegalStateException("inventory_command_unresolved_http_"+response.statusCode());
        try{
            JsonNode receipt=json.readTree(response.body());
            if(!command.commandId().equals(receipt.path("commandId").asText()) || !command.orderId().equals(receipt.path("orderId").asText())
                || !command.hash().equals(receipt.path("requestHash").asText()) || !java.util.Set.of("APPLIED","REJECTED").contains(receipt.path("status").asText()))
                throw new IllegalStateException("inventory_receipt_identity_mismatch");
            return receipt;
        }catch(Exception invalid){throw new IllegalStateException("invalid_inventory_receipt",invalid);}
    }
}
