package com.example.locallife.ordering;

/** Read-only catalog boundary used by the trade domain. */
public interface CommerceCatalogPort {
    CommerceItemSnapshot requireItem(String itemType, Long itemId);
}
