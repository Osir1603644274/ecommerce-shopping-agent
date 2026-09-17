package com.example.locallife.ordering;

import java.math.BigInteger;
import java.util.*;

/** Integer minor-unit arithmetic. Stable input order breaks equal-remainder ties. */
public final class MoneyAllocation {
    private MoneyAllocation() { }
    public static List<Long> discount(List<Long> subtotals, long discount) {
        if (subtotals.isEmpty() || discount < 0 || subtotals.stream().anyMatch(v -> v == null || v < 0))
            throw new IllegalArgumentException("Invalid allocation amounts");
        long total = 0;
        for (long value : subtotals) total = Math.addExact(total, value);
        if (discount > total) throw new IllegalArgumentException("Discount exceeds total");
        if (total == 0) return subtotals.stream().map(v -> 0L).toList();
        BigInteger denominator = BigInteger.valueOf(total);
        long[] result = new long[subtotals.size()];
        BigInteger[] remainders = new BigInteger[result.length];
        long allocated = 0;
        for (int i = 0; i < result.length; i++) {
            var qr = BigInteger.valueOf(subtotals.get(i)).multiply(BigInteger.valueOf(discount)).divideAndRemainder(denominator);
            result[i] = qr[0].longValueExact(); remainders[i] = qr[1]; allocated += result[i];
        }
        var indexes = new ArrayList<Integer>();
        for (int i = 0; i < result.length; i++) indexes.add(i);
        indexes.sort(Comparator.<Integer, BigInteger>comparing(i -> remainders[i]).reversed().thenComparingInt(i -> i));
        for (int i = 0; i < discount - allocated; i++) result[indexes.get(i)]++;
        return Arrays.stream(result).boxed().toList();
    }

    /** Refund the next units; a line's remainder cents belong to its first units. */
    public static long refund(long paid, int quantity, int alreadyRefunded, int requested) {
        if (paid < 0 || quantity <= 0 || alreadyRefunded < 0 || requested <= 0 || requested > quantity - alreadyRefunded)
            throw new IllegalArgumentException("Invalid refund quantity");
        long base = paid / quantity, remainder = paid % quantity;
        return Math.addExact(Math.multiplyExact(base, requested),
                Math.min((long) alreadyRefunded + requested, remainder) - Math.min(alreadyRefunded, remainder));
    }
}
