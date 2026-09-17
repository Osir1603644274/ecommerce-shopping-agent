"""Read-only protocol check for the shared local demo/Codex service."""
import asyncio
import json
from pathlib import Path
from mcp import Client


async def main():
    manifest=json.loads((Path(__file__).resolve().parents[3]/'datasets/knowledge/phone-v2/manifest.json').read_text(encoding='utf8'))
    async with asyncio.timeout(15):
        async with Client('http://127.0.0.1:18791/mcp') as client:
            tools=await client.list_tools()
            assert {t.name for t in tools.tools}=={'search_product_evidence','get_product_evidence'}
            result=await client.call_tool('search_product_evidence',dict(query='iPhone13芯片',model_keys=['apple:13'],fields=['chip']))
            data=result.model_dump(by_alias=True)
            value=data.get('structuredContent') or json.loads(next(c.text for c in result.content if c.type=='text'))
            assert not data.get('isError') and value['knowledgeVersion']==manifest['version'] and value['evidence']
    print(json.dumps(dict(status='OK',knowledgeVersion=manifest['version']),ensure_ascii=False))


if __name__=='__main__':asyncio.run(main())
