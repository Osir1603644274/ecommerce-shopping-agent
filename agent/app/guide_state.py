"""The canonical, source-independent shopping request kept in TaskState.

Search evidence and purchasable offers deliberately live outside this model.
The model may propose a turn; only these pure transitions may change the
persisted guide.  Keeping the transition pure lets callers rebuild it after a
TaskState revision conflict without replaying a model response as a write.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


GuideAction = Literal["search", "refine", "new", "undo", "compare", "cancel", "inspect", "clarify"]
GuideRoute = Literal["catalog", "product", "business"]
_guide_writer: ContextVar[bool] = ContextVar("guide_writer", default=False)
PROTECTED_GUIDE_KEYS = frozenset({"shopping", "shoppingMigration", "guideV1", "guideV1Migration", "guideTurnDecision", "guideEvidenceRef"})
LEGACY_READ_UNTIL = datetime(2026, 10, 8, tzinfo=timezone.utc)


@contextmanager
def bind_guide_write():
    token = _guide_writer.set(True)
    try:
        yield
    finally:
        _guide_writer.reset(token)


def guide_write_authorized() -> bool:
    return _guide_writer.get()


class GuideRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facet: str = Field(min_length=1, max_length=80)
    operator: Literal["eq", "lte", "gte", "in", "not_in"]
    value: int | float | str | bool | list[str]
    unit: str = Field(default="", max_length=40)
    priority: Literal["hard", "soft"]
    source: str = Field(min_length=1, max_length=500)
    terms: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_value(self) -> "GuideRequirement":
        if isinstance(self.value, list) and (not self.value or len(set(self.value)) != len(self.value)):
            raise ValueError("guide requirement alternatives must be nonempty and unique")
        if self.operator in {"in", "not_in"} and not isinstance(self.value, list):
            raise ValueError("in/not_in requires a value list")
        if self.operator in {"lte", "gte"} and (type(self.value) not in {int, float}):
            raise ValueError("numeric comparison requires a number")
        return self


class GuideSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    query: str = Field(default="", max_length=1000)
    retrieval_query: str = Field(default="", alias="retrievalQuery", max_length=1000)
    requirements: list[GuideRequirement] = Field(default_factory=list, max_length=16)


class GuideV1(GuideSnapshot):
    schema_version: Literal[1] = Field(default=1, alias="schemaVersion")
    history: list[GuideSnapshot] = Field(default_factory=list, max_length=10)


class ShoppingState(GuideSnapshot):
    """Only current shopping demand; display evidence is stored separately."""

    history: list[GuideSnapshot] = Field(default_factory=list, max_length=10)


class GuideTurn(BaseModel):
    """One model decision.  Display numbers are hints, never product IDs."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    route: GuideRoute
    action: GuideAction
    query: str = Field(default="", max_length=1000)
    retrieval_query: str = Field(default="", alias="retrievalQuery", max_length=1000)
    requirements: list[GuideRequirement] = Field(default_factory=list, max_length=16)
    numbers: list[StrictInt] = Field(default_factory=list, max_length=6)
    question: str = Field(default="", max_length=300)
    followup: Literal["none", "detail", "included", "accessory", "ambiguous"] = "none"
    accessory: str = Field(default="", max_length=80)
    reference_model: str = Field(default="", alias="referenceModel", max_length=80)

    @model_validator(mode="after")
    def validate_route_action(self) -> "GuideTurn":
        if self.route == "business" and self.action != "inspect":
            raise ValueError("business turns must use inspect")
        if self.route == "catalog" and self.action in {"search", "refine", "new"} and not self.query.strip():
            raise ValueError("search turns require a query")
        if self.action == "compare" and (len(self.numbers) < 2 or len(set(self.numbers)) != len(self.numbers)):
            raise ValueError("compare requires distinct display numbers")
        return self


def transition(current: GuideV1 | ShoppingState, turn: GuideTurn) -> GuideV1 | ShoppingState:
    """Apply semantic changes; evidence invalidation is the caller's duty."""
    if turn.route != "catalog" or turn.action in {"compare", "clarify", "inspect"}:
        return current
    if turn.action == "cancel":
        return type(current)()
    if turn.action == "undo":
        if not current.history:
            return current
        previous = current.history[-1]
        return type(current)(**previous.model_dump(by_alias=True), history=current.history[:-1])
    previous = GuideSnapshot(
        query=current.query,
        retrievalQuery=current.retrieval_query,
        requirements=deepcopy(current.requirements),
    )
    return type(current)(
        query=turn.query.strip(),
        retrievalQuery=turn.retrieval_query.strip() or turn.query.strip(),
        requirements=deepcopy(turn.requirements),
        history=[] if turn.action == "new" else ([*current.history, previous][-10:]
            if current.query or current.requirements else list(current.history)),
    )


def catalog_projection(guide: GuideV1 | ShoppingState, *, scope: dict[str, Any] | None = None,
                       revision: int = 0) -> dict[str, Any]:
    """Compatibility view for the unchanged retrieval and answer functions.

    The caller may cache this view for UI rendering, but must never use it as
    the authority for subsequent user requirements.
    """
    from .catalog_service import verify_scope

    verify_scope(scope)
    if scope is not None and scope.get("query") != guide.query:
        raise ValueError("document scope belongs to a different guide query")

    def legacy_requirement(item: GuideRequirement) -> dict[str, Any]:
        negative = item.operator == "not_in"
        mode = ("exclude" if item.priority == "hard" else "avoid") if negative else (
            "require" if item.priority == "hard" else "prefer"
        )
        value = ",".join(item.value) if isinstance(item.value, list) else str(item.value)
        if item.operator in {"lte", "gte"}:
            value += item.unit + ("以内" if item.operator == "lte" else "以上")
        return {"facet": item.facet, "mode": mode, "value": value, "terms": list(item.terms)}

    return {
        "revision": revision,
        "query": guide.query,
        "retrievalQuery": guide.retrieval_query,
        "requirements": [legacy_requirement(item) for item in guide.requirements],
        "history": [{"query": item.query, "retrievalQuery": item.retrieval_query,
                     "requirements": [legacy_requirement(r) for r in item.requirements],
                     "scope": None} for item in guide.history],
        "scope": scope,
    }


def _catalog_requirements(rows: Any) -> list[GuideRequirement] | None:
    if not isinstance(rows, list):
        return None
    requirements = []
    mode_map = {
        "require": ("eq", "hard"), "exclude": ("not_in", "hard"),
        "prefer": ("eq", "soft"), "avoid": ("not_in", "soft"),
    }
    for row in rows:
        if not isinstance(row, dict) or row.get("mode") not in mode_map or not isinstance(row.get("value"), str):
            return None
        operator, priority = mode_map[row["mode"]]
        requirements.append(GuideRequirement(
            facet=row["facet"], operator=operator,
            value=[row["value"]] if operator == "not_in" else row["value"],
            priority=priority, source="migration:catalogSearch", terms=row.get("terms", []),
        ))
    return requirements


def from_catalog_search(raw: Any) -> GuideV1 | None:
    """Read-only legacy migration; never infer missing operators or evidence."""
    if not isinstance(raw, dict) or not isinstance(raw.get("query"), str):
        return None
    from .catalog_service import verify_scope

    verify_scope(raw.get("scope"))
    requirements = _catalog_requirements(raw.get("requirements", []))
    if requirements is None:
        return None
    raw_history = raw.get("history", [])
    if not isinstance(raw_history, list):
        return None
    history = []
    for item in raw_history[-10:]:
        if not isinstance(item, dict) or not isinstance(item.get("query"), str):
            return None
        verify_scope(item.get("scope"))
        old_requirements = _catalog_requirements(item.get("requirements", []))
        if old_requirements is None:
            return None
        history.append(GuideSnapshot(query=item["query"],
                                     retrievalQuery=item.get("retrievalQuery") or item["query"],
                                     requirements=old_requirements))
    return GuideV1(query=raw["query"], retrievalQuery=raw.get("retrievalQuery") or raw["query"],
                   requirements=requirements, history=history)


def _partial_catalog_migration(raw: dict[str, Any]) -> tuple[GuideV1, list[Any]]:
    """Keep valid old facts without silently treating malformed facts as false."""
    query = raw.get("query") if isinstance(raw.get("query"), str) else ""
    converted: list[GuideRequirement] = []
    unconverted: list[Any] = []
    for row in raw.get("requirements", []) if isinstance(raw.get("requirements"), list) else []:
        try:
            parsed = _catalog_requirements([row])
            if parsed is None:
                raise ValueError("unconvertible catalog requirement")
            converted.extend(parsed)
        except (TypeError, ValueError, KeyError):
            unconverted.append(deepcopy(row))
    if not isinstance(raw.get("requirements", []), list):
        unconverted.append(deepcopy(raw.get("requirements")))
    history: list[GuideSnapshot] = []
    for item in raw.get("history", [])[-10:] if isinstance(raw.get("history"), list) else []:
        try:
            migrated = from_catalog_search({**item, "scope": None, "history": []})
        except (TypeError, ValueError, KeyError):
            migrated = None
        if migrated is not None:
            history.append(GuideSnapshot(query=migrated.query,
                                         retrievalQuery=migrated.retrieval_query,
                                         requirements=migrated.requirements))
        else:
            unconverted.append({"history": deepcopy(item)})
    if not isinstance(raw.get("history", []), list):
        unconverted.append({"history": deepcopy(raw.get("history"))})
    return GuideV1(query=query[:1000], retrievalQuery=(raw.get("retrievalQuery") or query)[:1000],
                   requirements=converted, history=history), unconverted


def from_shopping_guide(raw: Any, *, goal: str = "") -> GuideV1 | None:
    if not isinstance(raw, dict):
        return None
    from .domains.ecommerce.models import ShoppingGuideState

    guide = ShoppingGuideState.model_validate(raw)
    if guide.category is None:
        return None
    category_name = {"phone": "手机", "laptop": "笔记本电脑", "headphones": "耳机"}[guide.category]
    requirements = [GuideRequirement(facet="商品", operator="eq", value=category_name,
                                     priority="hard", source="migration:shoppingGuide:category")]
    for item in guide.requirements:
        facet, value, unit = item.key, item.value, item.unit
        if item.key == "price_minor" and item.unit == "CNY_MINOR" and type(item.value) in {int, float}:
            facet, value, unit = "预算", item.value / 100, "元"
            if value == int(value):
                value = int(value)
        requirements.append(GuideRequirement(
            facet=facet, operator=item.operator, value=value, unit=unit,
            priority=item.priority, source=item.source,
        ))
    for avoidance in guide.brand_avoidances:
        requirements.append(GuideRequirement(
            facet="品牌", operator="not_in", value=list(avoidance.values),
            priority=avoidance.strength, source="migration:shoppingGuide:brandAvoidances",
        ))
    return GuideV1(query=goal[:1000], retrievalQuery=goal[:1000], requirements=requirements)


async def ensure_guide_v1(task: Any, *, catalog_search: Any = None) -> Any:
    """Migrate an active session once; return the winning TaskState revision.

    A malformed or stale document scope is never copied into TaskState.  Its
    visible cards must be re-established by a subsequent search.
    """
    from .task_state import (
        TaskStatePatchRequest, TaskStateRevisionConflictError,
        get_task_state, update_task_state,
    )

    for _ in range(2):
        if isinstance(task.domain_state.get("guideV1"), dict):
            GuideV1.model_validate(task.domain_state["guideV1"])
            return task
        migrated = None
        source = None
        requery = False
        unconverted: list[Any] = []
        if isinstance(catalog_search, dict):
            try:
                migrated = from_catalog_search(catalog_search)
            except (TypeError, ValueError):
                # Keep the demand, but never carry an unverifiable scope.
                cleaned = deepcopy(catalog_search)
                cleaned["scope"] = None
                old_history = cleaned.get("history", [])
                for item in (old_history if isinstance(old_history, list) else []):
                    if isinstance(item, dict):
                        item["scope"] = None
                migrated = from_catalog_search(cleaned)
                requery = True
            if migrated is None:
                migrated, unconverted = _partial_catalog_migration(catalog_search)
                requery = True
            if migrated is not None:
                source = "catalogSearch"
                requery = requery or bool(catalog_search.get("scope"))
        if migrated is None:
            try:
                migrated = from_shopping_guide(
                    task.domain_state.get("shoppingGuide"), goal=task.goal,
                )
            except (TypeError, ValueError):
                requery = True
            if migrated is not None:
                source = "shoppingGuide"
                requery = True
                old = task.domain_state.get("shoppingGuide") or {}
                if old.get("useCases"):
                    unconverted.append({"useCases": deepcopy(old["useCases"])})
        if migrated is None:
            migrated = GuideV1(query=task.goal[:1000], retrievalQuery=task.goal[:1000])
            source = "taskGoal"
            requery = True
        patch = TaskStatePatchRequest(
            expectedRevision=task.revision, actor="system",
            domainStatePatch={
                "guideV1": migrated.model_dump(by_alias=True, mode="json"),
                "guideV1Migration": {"source": source, "needsRequery": requery,
                                     "needsClarification": bool(unconverted),
                                     "unconverted": unconverted[:16]},
            },
        )
        try:
            with bind_guide_write():
                return await update_task_state(task.task_id, patch)
        except TaskStateRevisionConflictError:
            latest = await get_task_state(task.task_id)
            if latest is None:
                raise
            task = latest
    raise RuntimeError("guide_v1_migration_revision_conflict")


async def ensure_shopping_state(task: Any, *, catalog_search: Any = None) -> Any:
    """Idempotently migrate live legacy demand; never resurrect archived messages.

    Old fields are inputs only and expire after the 14-day read window. The
    current turn's goal is deliberately not interpreted as prior history.
    """
    from .task_state import (TaskStatePatchRequest, TaskStateRevisionConflictError,
                             get_task_state, update_task_state)

    for _ in range(2):
        raw = task.domain_state.get("shopping")
        if isinstance(raw, dict):
            ShoppingState.model_validate(raw)
            return task
        migrated: ShoppingState | None = None
        source = "new"
        requery = False
        unconverted: list[Any] = []
        legacy_allowed = datetime.now(timezone.utc) < LEGACY_READ_UNTIL
        if legacy_allowed:
            old_guide = task.domain_state.get("guideV1")
            if isinstance(old_guide, dict):
                try:
                    old = GuideV1.model_validate(old_guide)
                    migrated = ShoppingState(**old.model_dump(by_alias=True,
                        exclude={"schema_version"}))
                    source = "guideV1"
                    requery = True
                    old_marker = task.domain_state.get("guideV1Migration") or {}
                    unconverted = list(old_marker.get("unconverted") or [])[:16]
                except ValueError:
                    unconverted.append({"guideV1": deepcopy(old_guide)})
            if migrated is None and isinstance(catalog_search, dict):
                try:
                    old = from_catalog_search(catalog_search)
                except (TypeError, ValueError):
                    old = None
                if old is None:
                    old, partial = _partial_catalog_migration(catalog_search)
                    unconverted.extend(partial)
                migrated = ShoppingState(**old.model_dump(by_alias=True,
                    exclude={"schema_version"}))
                source = "catalogSearch"
                requery = True
            if migrated is None:
                raw_guide = task.domain_state.get("shoppingGuide")
                if raw_guide is not None:
                    try:
                        old = from_shopping_guide(raw_guide, goal=task.goal)
                    except (TypeError, ValueError, KeyError):
                        old = None
                        unconverted.append({"shoppingGuide": deepcopy(raw_guide)})
                    if old is not None:
                        migrated = ShoppingState(**old.model_dump(by_alias=True,
                            exclude={"schema_version"}))
                        source = "shoppingGuide"
                        requery = True
                        if isinstance(raw_guide, dict) and raw_guide.get("useCases"):
                            unconverted.append({"useCases": deepcopy(raw_guide["useCases"])})
        else:
            source = "legacy_expired"
            requery = True
        if migrated is None:
            migrated = ShoppingState()
        patch = TaskStatePatchRequest(expectedRevision=task.revision, actor="system",
            domainStatePatch={"shopping": migrated.model_dump(by_alias=True, mode="json"),
                "shoppingMigration": {"source": source, "needsRequery": requery,
                    "needsClarification": bool(unconverted), "unconverted": unconverted[:16]}})
        try:
            with bind_guide_write():
                return await update_task_state(task.task_id, patch)
        except TaskStateRevisionConflictError:
            latest = await get_task_state(task.task_id)
            if latest is None:
                raise
            task = latest
    raise RuntimeError("shopping_migration_revision_conflict")
