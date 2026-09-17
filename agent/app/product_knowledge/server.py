"""One loopback Streamable HTTP knowledge service, shared by Agent and Codex."""
import argparse
import os
from pathlib import Path

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from .store import KnowledgeStore


def build_server(directory: str | Path, *, embedder=None) -> MCPServer:
    store=KnowledgeStore(directory,embedder=embedder)
    server=MCPServer('product-knowledge',version=store.version,
        instructions='只读手机型号证据库。先search_product_evidence，再按evidenceId读取详情。仅使用返回证据；保留型号版本、来源与未知。型号资料不能证明实物机况或交易状态。')
    annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False,idempotentHint=True)

    @server.tool(annotations=annotations)
    def search_product_evidence(query: str, item_ids: list[str] | None = None,
                                model_keys: list[str] | None = None, fields: list[str] | None = None, top_k: int = 8, context_query: str | None = None) -> dict:
        """检索指定商品/型号的规格与实测证据；商品ID必须用字符串以保留精度。"""
        return store.search(query,item_ids=item_ids,model_keys=model_keys,fields=fields,top_k=top_k,context_query=context_query)

    @server.tool(annotations=annotations)
    def get_product_evidence(evidence_id: str) -> dict:
        """读取搜索返回的证据ID及完整来源、适用版本和条件。"""
        return store.evidence(evidence_id)

    return server


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--directory',default=str(Path(__file__).resolve().parents[3]/'datasets/knowledge/phone-v2'))
    parser.add_argument('--port',type=int,default=18791)
    parser.add_argument('--no-bge',action='store_true',help='explicit BM25-only degraded mode')
    args=parser.parse_args()
    embedder=None
    if not args.no_bge:
        from fastembed import TextEmbedding
        embedder=TextEmbedding(model_name='BAAI/bge-small-zh-v1.5',
            cache_dir=os.environ.get('RAG_MODEL_CACHE_DIR',str(Path(__file__).resolve().parents[2]/'.cache/fastembed')),
            threads=2)
    server=build_server(args.directory,embedder=embedder)
    server.run(transport='streamable-http',host='127.0.0.1',port=args.port,stateless_http=True,json_response=True)


if __name__=='__main__': main()
