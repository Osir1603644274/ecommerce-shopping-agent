package com.example.locallife.flashsale;

/** Optional test instrumentation. No implementation is shipped in the production jar. */
public interface FlashSaleFaultProbe {
    void at(String point, String requestId);
}
