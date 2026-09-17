from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.llm import _forced_place_arguments, run_agent, select_tool_schemas
from app.schemas import ToolTrace
from app.tools import TOOL_SCHEMAS, call_tool, get_place_detail_tool, search_places_tool


def _tool_names(schemas: list[dict]) -> list[str]:
    return [schema["function"]["name"] for schema in schemas]


def _make_response(content=None, tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _make_tool_call(call_id: str, name: str, arguments: dict):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments, ensure_ascii=False)),
    )


def _fake_client(create_mock):
    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create_mock))
    )


class PlaceToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Place contracts have not migrated to the persisted Harness yet; this
        # suite explicitly verifies the rollback implementation.
        self._legacy_mode = patch("app.llm.settings.agent_orchestrator_mode", "legacy")
        self._legacy_mode.start()
        self.addCleanup(self._legacy_mode.stop)

    def test_exposes_place_search_and_detail_schemas(self):
        schemas = {item["function"]["name"]: item["function"] for item in TOOL_SCHEMAS}

        self.assertIn("search_places", schemas)
        self.assertIn("get_place_detail", schemas)
        self.assertEqual(
            schemas["get_place_detail"]["parameters"]["required"],
            ["placeId"],
        )
        self.assertEqual(
            schemas["search_places"]["parameters"]["properties"]["kind"]["enum"],
            ["park", "museum"],
        )

    async def test_search_places_returns_structured_snapshot_results(self):
        result = await search_places_tool(
            district=" 海淀区 ",
            park_type="综合公园",
            park_level="一级",
            limit=3,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.tool, "search_places")
        self.assertEqual(result.detail["catalogVersion"], "beijing-places-v2")
        self.assertEqual(result.detail["scope"], "demo_snapshot_not_realtime")
        self.assertIn("当前演示目录中找到N个", result.detail["answerConstraint"])
        self.assertEqual(result.detail["total"], 7)
        self.assertEqual(result.detail["count"], 3)
        self.assertTrue(all(item["district"] == "海淀区" for item in result.detail["items"]))
        self.assertTrue(all(item["coordinateSystem"] == "BD-09" for item in result.detail["items"]))

    async def test_dispatches_all_place_search_arguments(self):
        expected = ToolTrace(tool="search_places", ok=True, detail={"count": 0, "items": []})
        arguments = {
            "query": "朝阳公园",
            "district": "朝阳区",
            "kind": "park",
            "parkType": "综合公园",
            "parkLevel": "一级",
            "facilityName": "无障碍厕所/厕位",
            "facilityStatus": "标准",
            "signageStatus": "标准",
            "limit": 5,
        }
        with patch(
            "app.tools.search_places_tool",
            new=AsyncMock(return_value=expected),
        ) as place_search:
            result = await call_tool("search_places", arguments)

        self.assertEqual(result, expected)
        place_search.assert_awaited_once_with(
            query="朝阳公园",
            district="朝阳区",
            kind="park",
            park_type="综合公园",
            park_level="一级",
            facility_name="无障碍厕所/厕位",
            facility_status="标准",
            signage_status="标准",
            limit=5,
        )

    async def test_rejects_invalid_place_parameters(self):
        invalid_limit = await call_tool("search_places", {"limit": 0})
        invalid_district = await call_tool("search_places", {"district": 123})
        missing_facility = await call_tool(
            "search_places", {"facilityStatus": "标准"}
        )

        self.assertFalse(invalid_limit.ok)
        self.assertIn("limit", invalid_limit.detail)
        self.assertFalse(invalid_district.ok)
        self.assertIn("district", invalid_district.detail)
        self.assertFalse(missing_facility.ok)
        self.assertIn("facility_name", missing_facility.detail)

    async def test_get_place_detail_returns_source_aware_detail(self):
        result = await get_place_detail_tool(" beijing-park-433 ")

        self.assertTrue(result.ok)
        self.assertEqual(result.detail["place"]["name"], "北京世界公园")
        self.assertEqual(result.detail["place"]["parkLevel"], "二级")
        self.assertEqual(result.detail["place"]["coordinateSystem"], "BD-09")
        self.assertEqual(
            result.detail["place"]["sourceRefs"]["official_parks"],
            "433",
        )

    async def test_search_and_get_museum_detail(self):
        search = await search_places_tool(query="故宫博物院", kind="museum")

        self.assertTrue(search.ok)
        self.assertEqual(search.detail["total"], 1)
        self.assertEqual(search.detail["items"][0]["id"], "beijing-museum-1")

        detail = await get_place_detail_tool("beijing-museum-1")
        self.assertTrue(detail.ok)
        self.assertEqual(detail.detail["place"]["address"], "北京市东城区景山前街4号")
        self.assertEqual(detail.detail["place"]["phone"], "65132255")
        self.assertEqual(detail.detail["place"]["postcode"], "100009")

    async def test_get_place_detail_handles_missing_or_invalid_id(self):
        missing = await get_place_detail_tool("beijing-park-missing")
        invalid = await call_tool("get_place_detail", {})

        self.assertFalse(missing.ok)
        self.assertEqual(missing.detail["placeId"], "beijing-park-missing")
        self.assertFalse(invalid.ok)
        self.assertIn("placeId", invalid.detail)


class PlaceRouterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._legacy_mode = patch("app.llm.settings.agent_orchestrator_mode", "legacy")
        self._legacy_mode.start()
        self.addCleanup(self._legacy_mode.stop)

    def test_routes_park_facts_to_place_tools(self):
        self.assertEqual(
            _tool_names(select_tool_schemas("海淀区有哪些一级综合公园？")),
            ["search_places", "get_place_detail"],
        )
        self.assertEqual(
            _tool_names(select_tool_schemas("北京世界公园的电话是多少？")),
            ["search_places", "get_place_detail"],
        )

    def test_park_subjective_question_does_not_use_merchant_review_rag(self):
        self.assertEqual(
            _tool_names(select_tool_schemas("朝阳公园适合老人散步吗？")),
            ["search_places", "get_place_detail"],
        )

    def test_routes_museum_facts_to_the_shared_place_tools(self):
        self.assertEqual(
            _tool_names(select_tool_schemas("故宫博物院的地址和电话是什么？")),
            ["search_places", "get_place_detail"],
        )
        self.assertEqual(
            _forced_place_arguments("东城区有哪些博物馆？"),
            {"kind": "museum", "district": "东城区"},
        )

    def test_shop_near_a_park_keeps_shop_route(self):
        self.assertEqual(
            _tool_names(select_tool_schemas("朝阳公园附近有哪些咖啡店？")),
            ["search_shops"],
        )
        self.assertEqual(
            _tool_names(select_tool_schemas("咖啡店有没有无障碍设施？")),
            ["search_shop_reviews", "search_knowledge"],
        )

    async def test_place_question_has_search_fallback_with_structured_filters(self):
        first = _make_response(content="海淀区有一些一级综合公园。")
        second = _make_response(content="演示目录中找到7个，例如西小口公园。")
        create_mock = AsyncMock(side_effect=[first, second])
        trace = ToolTrace(
            tool="search_places",
            ok=True,
            detail={"total": 7, "count": 3, "items": [{"name": "西小口公园"}]},
        )

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)), patch(
            "app.llm.call_tool", new=AsyncMock(return_value=trace)
        ) as call_tool_mock:
            answer, traces, _, _run_id, _summary = await run_agent("海淀区有哪些一级综合公园？")

        self.assertIn("西小口公园", answer)
        self.assertEqual(traces, [trace])
        first_request = create_mock.await_args_list[0].kwargs
        self.assertIn("tools", first_request)
        if "tool_choice" in first_request:
            self.assertEqual(first_request["tool_choice"], "required")
        call_tool_mock.assert_awaited_once_with(
            "search_places",
            {
                "kind": "park",
                "district": "海淀区",
                "parkType": "综合公园",
                "parkLevel": "一级",
            },
        )

    async def test_can_chain_place_search_then_detail(self):
        search_call = _make_tool_call(
            "call_place_search", "search_places", {"query": "北京世界公园"}
        )
        detail_call = _make_tool_call(
            "call_place_detail", "get_place_detail", {"placeId": "beijing-park-433"}
        )
        create_mock = AsyncMock(
            side_effect=[
                _make_response(tool_calls=[search_call]),
                _make_response(tool_calls=[detail_call]),
                _make_response(content="北京世界公园位于丰台区花乡大葆台158号。"),
            ]
        )
        search_trace = ToolTrace(
            tool="search_places",
            ok=True,
            detail={"items": [{"id": "beijing-park-433", "name": "北京世界公园"}]},
        )
        detail_trace = ToolTrace(
            tool="get_place_detail",
            ok=True,
            detail={"place": {"id": "beijing-park-433", "address": "丰台区花乡大葆台158号"}},
        )

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)), patch(
            "app.llm.call_tool",
            new=AsyncMock(side_effect=[search_trace, detail_trace]),
        ) as call_tool_mock:
            answer, traces, _, _rid, _sum = await run_agent("北京世界公园的地址是什么？")

        self.assertIn("丰台区", answer)
        self.assertEqual(
            [trace.tool for trace in traces],
            ["search_places", "get_place_detail"],
        )
        self.assertEqual(
            call_tool_mock.await_args_list[0].args,
            ("search_places", {"query": "北京世界公园", "kind": "park"}),
        )
        self.assertEqual(
            call_tool_mock.await_args_list[1].args,
            ("get_place_detail", {"placeId": "beijing-park-433"}),
        )


if __name__ == "__main__":
    unittest.main()
