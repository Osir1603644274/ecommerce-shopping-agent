package com.example.inventoryservice;

import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.server.ResponseStatusException;
import org.springframework.http.HttpStatus;
import static com.example.inventoryservice.InventoryProtocol.*;

@RestController
@RequestMapping("/internal/inventory")
public class InventoryController {
    private final InventoryCommands commands;
    public InventoryController(InventoryCommands commands){this.commands=commands;}
    @PostMapping("/commands") public Receipt apply(@Valid @RequestBody Command command){
        try{return commands.apply(command.normalized());}
        catch(IllegalArgumentException invalid){throw new ResponseStatusException(HttpStatus.BAD_REQUEST,invalid.getMessage());}
    }
    @GetMapping("/commands/{id}") public Receipt receipt(@PathVariable String id){
        return commands.receipt(id).orElseThrow(()->new ResponseStatusException(HttpStatus.NOT_FOUND));
    }
    @GetMapping("/stocks/{type}/{id}") public Stock stock(@PathVariable String type,@PathVariable Long id){
        return commands.stock(type,id).orElseThrow(()->new ResponseStatusException(HttpStatus.NOT_FOUND));
    }
    @PostMapping("/stocks/query") public java.util.List<Stock> stocks(@Valid @RequestBody StockQuery query){return commands.stocks(query);}
    @PostMapping("/stocks") public Stock createStock(@Valid @RequestBody Item item){return commands.createStock(item);}
}
