package com.example.inventoryservice;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.*;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.core.io.ClassPathResource;
import java.nio.charset.StandardCharsets;
import java.time.LocalDateTime;
import java.util.*;
import static com.example.inventoryservice.InventoryProtocol.*;
import static org.assertj.core.api.Assertions.*;

/** Real independent inventory transactions; H2 dialect adaptation is explicit, not MySQL certification. */
class ReturnedInventoryIntegrationTest {
    InventoryCommands commands;JdbcTemplate jdbc;
    @BeforeEach void setup() throws Exception {
        var data=new DriverManagerDataSource("jdbc:h2:mem:"+UUID.randomUUID()+";MODE=MySQL;DATABASE_TO_LOWER=TRUE;DB_CLOSE_DELAY=-1","sa","");
        jdbc=new JdbcTemplate(data);
        for(String path:List.of("db/migration/V1__owned_inventory.sql","db/migration/V2__returned_inventory.sql")) {
            String sql=new ClassPathResource(path).getContentAsString(StandardCharsets.UTF_8).replace("response_json JSON","response_json CLOB");
            new org.springframework.jdbc.datasource.init.ResourceDatabasePopulator(
                new org.springframework.core.io.ByteArrayResource(sql.getBytes(StandardCharsets.UTF_8))).execute(data);
        }
        commands=new InventoryCommands(jdbc,new ObjectMapper().findAndRegisterModules(),new DataSourceTransactionManager(data));
        commands.createStock(new Item("PRODUCT",1L,20));
        assertThat(commands.apply(new Command("reserve","order","RESERVE",List.of(new Item("PRODUCT",1L,3)),LocalDateTime.now().plusHours(1))).status()).isEqualTo("APPLIED");
        assertThat(commands.apply(new Command("confirm","order","CONFIRM",List.of(),null)).status()).isEqualTo("APPLIED");
    }
    Command returned(String key,String kind,int quantity) {return new Command(key,"order",kind,List.of(new Item("PRODUCT",1L,quantity)),null);}
    @Test void sellableReplayAndConflictingKeyHaveNoDuplicateEffects() {
        var request=returned("return","RETURN_SELLABLE",3);
        assertThat(commands.apply(request).status()).isEqualTo("APPLIED");
        assertThat(commands.apply(request)).isEqualTo(commands.receipt("return").orElseThrow());
        assertThat(commands.stock("PRODUCT",1L).orElseThrow().availableQuantity()).isEqualTo(20);
        assertThat(jdbc.queryForObject("SELECT status FROM inventory_reservation",String.class)).isEqualTo("RETURNED");
        assertThatThrownBy(()->commands.apply(returned("return","RETURN_QUARANTINE",3))).hasMessageContaining("inventory_command_key_conflict");
        assertThat(commands.apply(returned("another","RETURN_SELLABLE",1)).status()).isEqualTo("REJECTED");
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM inventory_return_receipt",Integer.class)).isEqualTo(1);
    }
    @Test void quarantinePreservesPhysicalAccountingAndCannotBeRestoredAgain() {
        assertThat(commands.apply(returned("damaged","RETURN_QUARANTINE",1)).status()).isEqualTo("APPLIED");
        var stock=commands.stock("PRODUCT",1L).orElseThrow();
        assertThat(stock.availableQuantity()).isEqualTo(17);
        assertThat(stock.soldQuantity()).isEqualTo(2);
        assertThat(stock.totalQuantity()).isEqualTo(19);
        assertThat(commands.apply(new Command("restore","order","RESTORE_ALL",List.of(),null)).status()).isEqualTo("REJECTED");
        assertThat(commands.apply(returned("excess","RETURN_SELLABLE",3)).status()).isEqualTo("REJECTED");
        assertThat(commands.stock("PRODUCT",1L).orElseThrow()).isEqualTo(stock);
        assertThat(commands.apply(returned("remaining","RETURN_SELLABLE",2)).status()).isEqualTo("APPLIED");
        assertThat(commands.stock("PRODUCT",1L).orElseThrow().availableQuantity()).isEqualTo(19);
    }
    @Test void replacementUsesSeparateReservationAndDurableShortageReceipt() {
        var until=LocalDateTime.now().plusDays(30);
        var shortage=new Command("replacement-reserve:short","replacement-short","RESERVE",List.of(new Item("PRODUCT",1L,18)),until);
        assertThat(commands.apply(shortage).reason()).isEqualTo("insufficient_stock");
        assertThat(commands.apply(shortage).status()).isEqualTo("REJECTED");
        var fresh=new Command("replacement-reserve:next","replacement-next","RESERVE",List.of(new Item("PRODUCT",1L,1)),until);
        assertThat(commands.apply(fresh).status()).isEqualTo("APPLIED");
        commands.apply(fresh);
        assertThat(commands.stock("PRODUCT",1L).orElseThrow().availableQuantity()).isEqualTo(16);
        assertThat(commands.stock("PRODUCT",1L).orElseThrow().reservedQuantity()).isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT quantity FROM inventory_reservation WHERE order_id='order'",Integer.class)).isEqualTo(3);
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM inventory_reservation WHERE order_id='replacement-next'",Integer.class)).isEqualTo(1);
        var dispatch=new Command("replacement-dispatch:next","replacement-next","CONFIRM",List.of(),null);
        assertThat(commands.apply(dispatch).status()).isEqualTo("APPLIED");
        commands.apply(dispatch);
        assertThat(commands.stock("PRODUCT",1L).orElseThrow().reservedQuantity()).isZero();
        assertThat(commands.stock("PRODUCT",1L).orElseThrow().soldQuantity()).isEqualTo(4);
        assertThat(commands.stock("PRODUCT",1L).orElseThrow().availableQuantity()).isEqualTo(16);
    }
}
