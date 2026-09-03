# Beijing place-search demo

This module is an earlier structured-search example for Beijing parks and museums. It is kept separate from the current ecommerce catalog and merchant-review RAG pipeline.

## Files

- `../app/place_data/data/beijing_places_v2.json`: 100 Beijing park records and 190 museum records.
- `../app/place_data/data/beijing_parks_v1.json`: the earlier park-only dataset.
- `eval/beijing_places_v2_cases.json`: structured search and detail-query cases.
- `eval/beijing_places_v2_report.json`: latest evaluation output.

## Build and evaluate

```powershell
python agent\scripts\publish_beijing_place_catalog.py
python agent\scripts\evaluate_beijing_place_catalog.py
```

## Design

`app.place_data.catalog.PlaceCatalog` is a file-backed fact service. It supports literal filters for place type, facility name, facility status and signage status without requiring MySQL, Redis or an embedding model.

The Agent uses two tools:

- `search_places`: applies structured filters and returns compact candidates.
- `get_place_detail`: returns details for a stable place ID returned by search.

The catalog contains snapshot data rather than live opening hours. Museum coordinates and real-time availability are not included.

## Tests

```powershell
$env:PYTHONPATH='agent'
python -m pytest agent\tests\test_place_catalog.py agent\tests\test_place_agent_integration.py -q
```
