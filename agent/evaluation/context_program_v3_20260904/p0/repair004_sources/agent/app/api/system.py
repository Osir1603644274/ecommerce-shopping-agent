from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

from ..schemas import HealthResponse
from ..settings import settings


def create_system_router(static_dir: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(
            static_dir / "index.html",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @router.get("/task-state-lab", include_in_schema=False)
    def task_state_lab() -> FileResponse:
        return FileResponse(
            static_dir / "task-state-lab.html",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @router.get("/agent-flow", include_in_schema=False)
    def agent_flow() -> FileResponse:
        return FileResponse(
            static_dir / "agent-flow.html",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @router.get("/commerce-demo", include_in_schema=False)
    def commerce_demo() -> FileResponse:
        return FileResponse(
            static_dir / "commerce-demo.html",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @router.get(
        "/health",
        response_model=HealthResponse,
        tags=["系统接口"],
        summary="健康检查",
        description="检查 Agent 服务是否已经启动。",
    )
    def health() -> HealthResponse:
        # REPAIR-004: the read-back needs the process's ACTUALLY loaded
        # non-secret retrieval config (never the API key / Redis URL / model
        # credentials / private paths / env).  `service/status` stays for
        # backward compatibility.
        return HealthResponse(
            service="本地生活智能体服务",
            status="运行中",
            backend_base_url=settings.backend_base_url,
            product_retrieval_mode=settings.product_retrieval_mode,
        )

    return router


__all__ = ["create_system_router"]
