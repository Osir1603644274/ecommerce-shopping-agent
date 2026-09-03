# Beijing demo place catalog

This directory contains the small, reviewed runtime snapshot used by the learning project. It is intentionally separate from raw downloads, generated staging data, the shop domain, and the RAG index.

## Files

- `../app/place_data/data/beijing_places_v2.json`: runtime catalog with 100 reviewed Beijing park candidates and 190 registered open-museum snapshot records.
- `../app/place_data/data/beijing_parks_v1.json`: retained v1 park-only snapshot.
- `eval/beijing_places_v2_cases.json`: deterministic structured-search and detail-query cases.
- `eval/beijing_places_v2_report.json`: latest deterministic evaluation result.

## Rebuild

```powershell
cd F:\agent
python agent\scripts\publish_beijing_place_catalog.py
python agent\scripts\evaluate_beijing_place_catalog.py
```

## Runtime boundary

`app.place_data.catalog.PlaceCatalog` is a file-backed structured fact service. It does not import `rag.py`, `knowledge/*`, `tools.py`, or `llm.py`, and it does not require MySQL, Redis, Qdrant, or an embedding model.

Facility filtering is literal and source-aware: `facility_status` and `signage_status` are separate filters and must be paired with `facility_name`. A facility row whose element status is `不涉及` is not silently presented as confirmed availability merely because its signage status differs.

## Agent bridge

The catalog is connected to the Agent through two thin tools:

- `search_places`: forwards structured filters to `PlaceCatalog.search()` and returns compact candidates.
- `get_place_detail`: returns `PlaceRecord.to_detail()` for a stable place ID obtained from search.

The tool wrappers live in `app/tools.py`. Place-intent menu selection and the evidence-required fallback live in `app/llm.py`. Park and museum questions get only these two tools; merchant-review RAG remains isolated. A question about shops near a landmark still uses the shop route.

Official park levels, addresses, contacts, coordinates, accessibility fields, and museum addresses, phones, and postcodes use these structured tools. Museums currently have no coordinates or live opening-hours feed. Subjective or real-time claims require separate evidence, so the Agent must state the evidence boundary instead of guessing.

The tool response always includes `scope=demo_snapshot_not_realtime` and a Chinese scope notice. The Agent prompt forbids turning park level or accessibility rows into unsupported subjective or real-time claims.

## Verification

```powershell
cd F:\agent
$env:PYTHONPATH='F:\agent\agent'
python -m pytest agent\tests\test_place_catalog.py agent\tests\test_place_agent_integration.py -q
```

The 2026-07-15 live demo suite passed 6/6, including `search_places -> get_place_detail` for the Palace Museum address and phone lookup.
