"""Actual MCP transport, not a direct Store call disguised as MCP."""
import asyncio
import json
from urllib.parse import urlsplit

from mcp import Client


async def search_evidence(url: str, query: str, item_ids: list[str], *, timeout: float = 5.0, context_query: str | None = None) -> dict:
    parsed=urlsplit(url)
    if parsed.scheme!='http' or parsed.hostname not in {'127.0.0.1','localhost','::1'}:
        raise ValueError('knowledge_endpoint_must_be_loopback')
    async with asyncio.timeout(timeout):
        async with Client(url) as client:
            tools=await client.list_tools()
            names={t.name for t in tools.tools}
            if names!={'search_product_evidence','get_product_evidence'}:
                raise ValueError('knowledge_mcp_surface_mismatch')
            for t in tools.tools:
                if not t.annotations or t.annotations.model_dump(by_alias=True).get('readOnlyHint') is not True:
                    raise ValueError('knowledge_mcp_not_readonly')
            result=await client.call_tool('search_product_evidence',dict(query=query,item_ids=item_ids,top_k=8,context_query=context_query))
            result_data=result.model_dump(by_alias=True)
            if result_data.get('isError'): raise ValueError('knowledge_mcp_tool_failed')
            value=result_data.get('structuredContent')
            if not isinstance(value,dict):
                value=json.loads(next(c.text for c in result.content if c.type=='text'))
            if 'knowledgeVersion' not in value or not isinstance(value.get('evidence'),list):
                raise ValueError('knowledge_mcp_invalid_result')
            # Domain identity is rechecked outside the transport too.
            allowed_models={m for b in value.get('bindings',[]) if b.get('itemId') in item_ids and b.get('status')=='CLEAR' for m in b.get('modelKeys',[])}
            if any(e.get('modelKey') not in allowed_models for e in value['evidence']):
                raise ValueError('knowledge_mcp_model_scope_mismatch')
            return {**value,'transport':'MCP_STREAMABLE_HTTP'}
