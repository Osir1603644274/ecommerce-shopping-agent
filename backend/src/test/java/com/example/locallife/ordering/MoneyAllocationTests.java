package com.example.locallife.ordering;

import org.junit.jupiter.api.Test;
import java.util.*;
import static org.assertj.core.api.Assertions.*;

class MoneyAllocationTests {
    @Test void largestRemainderAndStableTiesAreExact() {
        assertThat(MoneyAllocation.discount(List.of(1L,1L,1L),2)).containsExactly(1L,1L,0L);
        assertThat(MoneyAllocation.discount(List.of(303L,404L),5)).containsExactly(2L,3L);
        assertThat(MoneyAllocation.discount(List.of(0L,0L),0)).containsExactly(0L,0L);
        assertThat(MoneyAllocation.discount(List.of(Long.MAX_VALUE-10,10L),Long.MAX_VALUE-1))
                .containsExactly(Long.MAX_VALUE-11,10L);
    }
    @Test void splittingRefundsPreservesEveryCentIncludingFreeUnits() {
        long sum=0;
        for(int i=0;i<3;i++) sum+=MoneyAllocation.refund(301,3,i,1);
        assertThat(sum).isEqualTo(301);
        assertThat(MoneyAllocation.refund(1,3,0,1)).isEqualTo(1);
        assertThat(MoneyAllocation.refund(1,3,1,2)).isZero();
        assertThatThrownBy(()->MoneyAllocation.refund(1,3,1,3)).isInstanceOf(IllegalArgumentException.class);
    }
    @Test void seededCasesConserveDiscountAndRefundTotals() {
        Random random=new Random(20260905);
        for(int trial=0;trial<500;trial++) {
            List<Long> totals=List.of((long)random.nextInt(100000),(long)random.nextInt(100000),(long)random.nextInt(100000));
            long sum=totals.stream().mapToLong(Long::longValue).sum(), discount=random.nextLong(sum+1);
            var allocated=MoneyAllocation.discount(totals,discount);
            assertThat(allocated.stream().mapToLong(Long::longValue).sum()).isEqualTo(discount);
            for(int i=0;i<3;i++) {
                assertThat(allocated.get(i)).isBetween(0L,totals.get(i));
                long paid=totals.get(i)-allocated.get(i), refunded=0;
                int quantity=1+random.nextInt(40);
                for(int q=0;q<quantity;q++) refunded+=MoneyAllocation.refund(paid,quantity,q,1);
                assertThat(refunded).isEqualTo(paid);
            }
        }
    }
}
