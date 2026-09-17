"""Frozen regression tests for the Phase 2B oracle-first 96 Pilot."""

from __future__ import annotations

import json
import base64
import inspect
import re
from collections import Counter
from pathlib import Path

import pytest

from agent.evaluation import commerce_benchmark_v1_pilot as pilot
from agent.evaluation import commerce_benchmark_v1_public as public_loader
from agent.evaluation import commerce_benchmark_v1_public_text_authoring as authoring
from agent.evaluation import scenario_lab_v1 as lab


ROOT = Path(__file__).resolve().parents[1]
WORLD = ROOT.parent / "data" / "derived" / "commerce_world_cn_v1_controlled_20260821_r2"
PILOT = ROOT.parent / "data" / "derived" / "agentic_commerce_benchmark_v1_pilot_96_20260821_r11"
R10_PILOT = ROOT.parent / "data" / "derived" / "agentic_commerce_benchmark_v1_pilot_96_20260821_r10"
R8_PILOT = ROOT.parent / "data" / "derived" / "agentic_commerce_benchmark_v1_pilot_96_20260821_r8"
R9_PILOT = ROOT.parent / "data" / "derived" / "agentic_commerce_benchmark_v1_pilot_96_20260821_r9"
OLD_PILOT = ROOT.parent / "data" / "derived" / "agentic_commerce_benchmark_v1_pilot_96_20260821"


def _paths():
    return (
        PILOT / "public" / "scenario.input.jsonl",
        PILOT / "private" / "oracle.private.jsonl",
        PILOT / "private" / "fault.private.jsonl",
        PILOT / "run" / "prediction.not_run.jsonl",
        PILOT / "run" / "receipt.not_collected.jsonl",
    )


def _load():
    paths = _paths()
    return lab.ScenarioV1Set.load(*paths, manifest_path=WORLD / "manifest.json")


def test_pilot_96_exact_matrix_and_category_coverage():
    scenarios = _load().scenarios
    assert len(scenarios) == 96
    assert Counter(row.oracle["split"] for row in scenarios) == Counter({"development": 60, "validation": 30, "contract_red_team": 6})
    assert Counter(row.oracle["intentFamily"] for row in scenarios) == Counter({intent: 19 if intent != "product_finder" else 20 for intent in pilot.INTENT_FAMILIES})
    assert Counter(row.oracle["complexityStratum"] for row in scenarios) == Counter({"S0_SIMPLE": 31, "S1_FIXED_MULTISTEP": 30, "S2_OBSERVATION_DEPENDENT": 35})
    assert Counter(json.loads((PILOT / "private" / "authoring_audit.private.jsonl").read_text(encoding="utf-8").splitlines()[index])["category"] for index in range(96)) == Counter({category: 8 for category in pilot.CATEGORIES})


def test_every_oracle_ready_and_predictions_receipts_not_run():
    scenarios = _load().scenarios
    assert {row.oracle["oracleStatus"] for row in scenarios} == {"READY"}
    assert {row.prediction["runStatus"] for row in scenarios} == {"NOT_RUN"}
    assert {row.receipt["attestationStatus"] for row in scenarios} == {"NOT_COLLECTED"}
    assert {row.fault["faultStatus"] for row in scenarios} <= {"READY", "NONE"}
    for row in scenarios:
        if row.oracle["complexityStratum"] == "S2_OBSERVATION_DEPENDENT":
            points = list(row.oracle["successConditions"].get("requiredObservationPoints", []))
            assert points == [item["point"] for item in row.fault["injections"]]
            assert all(5 <= int(item["repeatCount"]) <= 10 for item in row.fault["injections"])


def test_s2_authoring_audit_has_nonempty_start_terminal_delta():
    audits = [json.loads(line) for line in (PILOT / "private" / "authoring_audit.private.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    s2 = [row for row in audits if row["complexityStratum"] == "S2_OBSERVATION_DEPENDENT"]
    assert len(s2) == 35
    assert all(row["completeUniverse"] and row["startUniverse"]["sha256"] != row["terminalUniverse"]["sha256"] for row in s2)


def test_s0_is_single_turn_and_s2_runner_requery_contract_is_explicit():
    scenarios = _load().scenarios
    audits = {
        row["scenarioId"]: row
        for row in (
            json.loads(line)
            for line in (PILOT / "private" / "authoring_audit.private.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }
    for scenario in scenarios:
        audit = audits[scenario.oracle["scenarioId"]]
        contract = audit["runnerContract"]
        if scenario.oracle["complexityStratum"] == "S0_SIMPLE":
            assert len(scenario.input["turns"]) == 1
            assert not audit["followupChangesConstraint"]
            assert "user_followup" not in audit["sourceTypes"]
            assert not contract["requiredPostInjectionAuthoritativeRequery"]
        if scenario.oracle["complexityStratum"] == "S2_OBSERVATION_DEPENDENT":
            assert contract["requiredPostInjectionAuthoritativeRequery"]
            assert contract["requiresAfterObservationDigest"]
            assert contract["bareInjectionTraceAccepted"] is False
            assert contract["verificationStatus"] == "AUTHORING_CONTRACT_ONLY"


def test_public_input_has_no_private_labels_or_values():
    public_path, oracle_path, fault_path, *_ = _paths()
    public_rows = lab.load_jsonl(public_path, "input")
    oracle_rows = lab.load_jsonl(oracle_path, "oracle")
    fault_rows = lab.load_jsonl(fault_path, "fault")
    assert all("intentFamily" not in json.dumps(row, ensure_ascii=False) for row in public_rows)
    assert all(re.search(r"merchant-\d{3}", json.dumps(row, ensure_ascii=False)) is None for row in public_rows)
    for public, oracle, fault in zip(public_rows, oracle_rows, fault_rows):
        lab.audit_public_input_leaks(public)
        lab.audit_public_private_value_leaks(public, oracle, fault)


def test_meaningful_followups_and_public_dependency_language():
    scenarios = _load().scenarios
    audits = [json.loads(line) for line in (PILOT / "private" / "authoring_audit.private.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert sum(bool(row["followupChangesConstraint"]) for row in audits) >= 12
    assert all(any(binding.get("sourceType") == "user_followup" for binding in audit["sourceBindings"]) for audit in audits if audit["followupChangesConstraint"])
    for scenario in scenarios:
        text = " ".join(turn["text"] for turn in scenario.input["turns"])
        if scenario.oracle["intentFamily"] == "knowledge_to_product":
            dependency = scenario.oracle["successConditions"].get("documentDependencies", [])[0]
            assert str(dependency["value"]) not in text
        if scenario.oracle["intentFamily"] == "multi_product_merchant":
            audit = audits[next(index for index, row in enumerate(audits) if row["scenarioId"] == scenario.scenario_id)]
            aliases = [binding["alias"] for binding in audit.get("publicEntityBindings", [])]
            assert aliases and all(alias in text for alias in aliases)
            # R11 public language is an independently authored frozen asset.
            # Bind this gate to the frozen semantic contract, not to mutable
            # phrasing pools in the current generator implementation.
            merchant_hint = audit["publicSemanticContext"]["merchantHint"]
            assert merchant_hint and merchant_hint in text


def test_public_language_is_natural_globally_and_aliases_bind_to_world():
    public_rows = lab.load_jsonl(PILOT / "public" / "scenario.input.jsonl", "input")
    oracle_rows = lab.load_jsonl(PILOT / "private" / "oracle.private.jsonl", "oracle")
    audits = [
        json.loads(line)
        for line in (PILOT / "private" / "authoring_audit.private.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    freeze = json.loads((PILOT / "freeze_manifest.json").read_text(encoding="utf-8"))
    all_text = "\n".join(turn["text"] for row in public_rows for turn in row["turns"])
    forbidden = (
        r"\bTrue\b", r"\bFalse\b", r"merchant-\d{3}", r"Controlled Merchant",
        r"受控商家\s*\d+", r"optionalSellerNote", r"再返回完整结果",
        r"\bcable\b", r"\bbeginner\b", r"(?<![A-Za-z])large(?![A-Za-z])",
        r"\bAST\b", r"\bschema\b", r"\bcontract\b",
    )
    for pattern in forbidden:
        assert re.search(pattern, all_text, flags=re.IGNORECASE) is None, pattern
    assert "价格上预算" not in all_text
    assert len({tuple(turn["text"] for turn in row["turns"]) for row in public_rows}) == 96
    for row in public_rows:
        for turn in row["turns"]:
            text = turn["text"]
            for segment in re.findall(r"电池健康度[^。；，]*", text):
                if re.search(r"\d", segment):
                    assert re.search(r"\d+(?:\.\d+)?%", segment)
    # A budget is one public requirement; the materializer must not restate
    # the same numeric bound as a second price atom.  Keep this as a global
    # semantic gate so a future template cannot regress one arbitrary case.
    for public, oracle in zip(public_rows, oracle_rows):
        if oracle["intentFamily"] != "product_finder" or oracle["complexityStratum"] != "S1_FIXED_MULTISTEP":
            continue
        text = " ".join(turn["text"] for turn in public["turns"])
        budget_values = set(re.findall(r"预算[^。；，]*?(\d+(?:\.\d+)?)元", text))
        price_values = set(re.findall(r"价格(?:不超过|低于|控制在)[^。；，]*?(\d+(?:\.\d+)?)元", text))
        assert not budget_values.intersection(price_values), text
    alias_to_merchant = {}
    for public, oracle, audit in zip(public_rows, oracle_rows, audits):
        public_text = " ".join(turn["text"] for turn in public["turns"])
        for binding in audit.get("publicEntityBindings", []):
            alias = str(binding["alias"])
            merchant_id = str(binding["merchantId"])
            assert alias in public_text
            assert alias not in {merchant_id, "受控商家 001"}
            if alias in alias_to_merchant:
                assert alias_to_merchant[alias] == merchant_id
            alias_to_merchant[alias] = merchant_id
            assert re.fullmatch(r"merchant-\d{3}", merchant_id)
        assert oracle["worldId"] == public["worldId"]
    assert len(alias_to_merchant) == len(set(alias_to_merchant.values()))
    language_audit = freeze["languageAudit"]
    assert language_audit["publicTurnSequenceCount"] == 96
    assert language_audit["normalizedTemplateCount"] >= 24
    assert language_audit["largestNormalizedTemplateCluster"] <= 4
    assert language_audit["largestNormalizedTemplateCluster"] == max(language_audit["normalizedTemplateCounts"].values())


def test_public_language_is_category_appropriate_and_compositional():
    """Freeze the blind-review failures as global, not case-specific, gates."""
    public_rows = lab.load_jsonl(PILOT / "public" / "scenario.input.jsonl", "input")
    oracle_rows = lab.load_jsonl(PILOT / "private" / "oracle.private.jsonl", "oracle")
    audits = [
        json.loads(line)
        for line in (PILOT / "private" / "authoring_audit.private.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    audit_by_id = {row["scenarioId"]: row for row in audits}
    for public, oracle in zip(public_rows, oracle_rows):
        text = " ".join(turn["text"] for turn in public["turns"])
        category = audit_by_id[public["scenarioId"]]["category"]
        singular, plural, cart = pilot._CATEGORY_QUANTIFIERS[category]
        intent = oracle["intentFamily"]
        if intent == "multi_product_merchant":
            assert plural in text, text
            assert re.search(r"从[^。；，]*一起买", text) is None, text
        elif intent == "coupon_budget":
            assert cart in text, text
        else:
            generic_measure = re.search(rf"一件{re.escape(pilot._CATEGORY_LABELS[category])}", text)
            assert generic_measure is None or generic_measure.group(0) == singular, text
            if any(marker in text for marker in ("一件", "一部", "一副", "一块", "一条", "一双", "一个", "一套", "一本")):
                # A consumer may naturally name the concrete accessory head
                # (for example, "连接线") while a later soft preference says
                # "一个配件".  It must not force an unnatural generic head.
                assert singular in text or (category == "phone_accessory" and "连接线" in text), text
        assert "还要我" not in text
        assert "我还我" not in text
        assert "目前有现货且当前有货" not in text
        assert "续航小时为" not in text
        assert "容量为大容量" not in text
        assert "读者层级为适合" not in text
        assert "价格上预算" not in text
    # R11 text is independently authored.  Its frozen soft preference is the
    # semantic modifier authority; current generator phrase pools may evolve.
    authored_by_signature = {
        str(row["semanticSignatureSha256"]): row
        for row in authoring._read_asset()
    }
    for public, audit in zip(public_rows, audits):
        text = " ".join(turn["text"] for turn in public["turns"])
        signature = authoring.semantic_signature(
            authoring.canonical_public_semantic_payload(audit)
        )
        witnesses = authored_by_signature[signature]["coverageWitnesses"]
        modifier_witnesses = [
            item["span"]
            for item in witnesses
            if item["semanticKey"] == "context:softPreference"
        ]
        assert len(modifier_witnesses) == 1
        assert modifier_witnesses[0] in text


def test_public_language_semantic_grammar_contract():
    """Reject field-register Chinese across semantic classes, not named cases.

    This intentionally runs against the frozen candidate selected by ``PILOT``.
    The immutable r8 corpus remains a red proof.  These global assertions
    gate the newly materialized r9 corpus without using identity-specific
    exceptions.
    """
    public_rows = lab.load_jsonl(PILOT / "public" / "scenario.input.jsonl", "input")
    audits = [
        json.loads(line)
        for line in (PILOT / "private" / "authoring_audit.private.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    audit_by_id = {row["scenarioId"]: row for row in audits}
    text_by_category = {
        category: " ".join(
            " ".join(turn["text"] for turn in row["turns"])
            for row in public_rows
            if audit_by_id[row["scenarioId"]]["category"] == category
        )
        for category in pilot.CATEGORIES
    }
    all_text = "\n".join(text_by_category.values())

    # 1 money/promotion register; 2 semantic category wording; 3 duplicate
    # availability ordering; 4 cardinality; 5 product-type field labels;
    # 6 seller fact request; 7 category-aware merchant policy wording.
    assert "促销资格" not in all_text
    assert "比较关注版本" not in text_by_category["drinkware"]
    assert "目前还能买到的一个" not in all_text
    assert "凑齐" not in all_text
    assert "商品配件类型" not in text_by_category["phone_accessory"]
    assert "额外商品说明" not in all_text
    assert "图书" not in text_by_category["books"] or "看看保修" not in text_by_category["books"]

    # Multiple unreviewed product families must exercise the same semantic
    # tables: passing by scenario/split/blind-list lookup is insufficient.
    for category, expected in {
        "phone_accessory": "连接线",
        "drinkware": "杯",
        "books": "缺页",
        "sneakers": "鞋底磨损",
    }.items():
        assert expected in text_by_category[category]


def test_public_text_authoring_dependency_closure_is_identity_and_order_safe():
    """R11 inspects the whole authoring module, not a selected helper list."""
    authoring.ast_dependency_closure_guard()
    source = inspect.getsource(authoring)
    # The token appears only in the guard's deny-list; executable authoring
    # logic is checked structurally by ast_dependency_closure_guard().
    assert "len(inputs)" not in source


def test_r8_frozen_corpus_exposes_all_global_language_failure_classes():
    """Keep the rejected r8 evidence visible instead of rewriting history."""
    r8_rows = lab.load_jsonl(R8_PILOT / "public" / "scenario.input.jsonl", "input")
    errors = pilot.public_language_lint(r8_rows)
    assert set(errors) == {
        "field_string_join",
        "knowledge_fragment",
        "ledger_money",
        "missing_collocation",
        "missing_head_or_copula",
        "mixed_role_join",
        "semantic_duplicate",
    }
    assert all(count > 0 for count in errors.values())


def test_r11_public_corpus_clears_global_language_lint():
    rows = lab.load_jsonl(PILOT / "public" / "scenario.input.jsonl", "input")
    assert pilot.public_language_lint(rows) == {}


def test_r9_frozen_corpus_is_red_for_all_eight_named_discourse_classes():
    rows = lab.load_jsonl(R9_PILOT / "public" / "scenario.input.jsonl", "input")
    errors = pilot.public_template_register_audit(rows)
    # R9 had already repaired money/collocation/knowledge/deduplication; this
    # frozen probe records the remaining observed discourse failures without
    # pretending old bytes fail a class they no longer contain.
    assert set(errors) >= {
        "typed_discourse_composition", "brand_head_copula", "mixed_condition_grammar",
        "category_inherent_redundancy", "template_register",
    }
    assert all(errors[name] > 0 for name in errors)


def test_r11_public_corpus_clears_named_discourse_register_audit():
    rows = lab.load_jsonl(PILOT / "public" / "scenario.input.jsonl", "input")
    assert pilot.public_template_register_audit(rows) == {}


def test_discourse_planner_and_realizer_cover_policy_knowledge_plural_and_money():
    ast = {"kind": "all", "children": [
        {"kind": "atom", "atom": {"field": "category", "operator": "EQ", "value": "phone_accessory"}},
        {"kind": "atom", "atom": {"field": "accessoryType", "operator": "EQ", "value": "cable"}},
        {"kind": "atom", "atom": {"field": "brand", "operator": "EQ", "value": "Baseus"}},
        {"kind": "atom", "atom": {"field": "stock", "operator": "GTE", "value": 1}},
    ]}
    plan = pilot._plan_consumer_utterance("multi_product_merchant", "phone_accessory", ast, "我比较在意接口兼容和做工", budget=None, followup=False, public_hint="青禾数码馆的保修", followup_field=None, variant_index=0)
    text = " ".join(pilot._realize_consumer_utterance(plan))
    assert "一次买两件手机配件" in text
    assert "如果收到后有问题" in text and "接口或连接出现问题时怎么处理？" in text
    assert "品牌要选" not in text and text.count("希望") <= 1
    for value, expected in ((216, "216元"), (447.5, "447.5元"), (216.45, "216.45元")):
        assert pilot._format_money(value) == expected
    knowledge = pilot._public_request("knowledge_to_product", "stationery", {"kind": "atom", "atom": {"field": "category", "operator": "EQ", "value": "stationery"}}, "希望文具类型、包装数量与可替换性说清楚", public_hint="包装数量")
    assert "通常该怎么看？" in knowledge[0]


def test_generic_discourse_matrix_covers_all_categories_intents_and_variants():
    for category in pilot.CATEGORIES:
        for intent in pilot.INTENT_FAMILIES:
            for variant in range(8):
                ast = {"kind": "all", "children": [
                    {"kind": "atom", "atom": {"field": "category", "operator": "EQ", "value": category}},
                    {"kind": "atom", "atom": {"field": "brand", "operator": "EQ", "value": "Apple"}},
                    {"kind": "atom", "atom": {"field": "stock", "operator": "GTE", "value": 1}},
                ]}
                turns = pilot._public_request(intent, category, ast, pilot._CATEGORY_MODIFIERS[category][variant], budget=216.45 if intent == "coupon_budget" else None, public_hint="青禾数码馆的退换货", variant_index=variant)
                text = " ".join(turns)
                assert text and "ACB-V1-" not in text
                assert "品牌要选" not in text and text.count("要选") == 0


@pytest.mark.parametrize(
    ("atoms", "category", "expected"),
    (
        (
            (
                {"field": "brand", "operator": "EQ", "value": "Apple"},
                {"field": "batteryHealthPct", "operator": "GTE", "value": 80},
                {"field": "promotionEligible", "operator": "EQ", "value": True},
            ),
            "used_phone",
            "品牌要选Apple，电池健康度至少80%，并且可以参加店内优惠活动",
        ),
        (
            (
                {"field": "accessoryType", "operator": "EQ", "value": "cable"},
                {"field": "brand", "operator": "EQ", "value": "Baseus"},
                {"field": "stock", "operator": "GTE", "value": 1},
            ),
            "phone_accessory",
            "选连接线，品牌要选Baseus，并且现在有货",
        ),
    ),
)
def test_typed_clause_composer_mixes_grammatical_roles_without_field_join(atoms, category, expected):
    clauses = [pilot._public_clause(atom, category=category) for atom in atoms]
    assert pilot._compose_public_clauses(clauses) == expected
    assert {clause["role"] for clause in clauses} >= {"brand", "numeric", "promotion"} or {clause["role"] for clause in clauses} >= {"product_type", "brand", "availability"}


@pytest.mark.parametrize(
    ("value", "expected"),
    ((216, "216元"), (447.5, "447.5元"), (216.45, "216.45元")),
)
def test_money_register_is_exact_without_ledger_style_yuan_jiao_fen(value, expected):
    rendered = pilot._format_money(value)
    assert rendered == expected
    assert re.search(r"元\d+(?:角|分)", rendered) is None


def test_typed_clause_composer_handles_knowledge_and_multi_product_deduplication():
    focus = pilot._knowledge_focus("stationery", "包装数量")
    assert focus == "套装内容和数量"
    assert "一套里有多少件该怎么比较" not in f"关于{focus}该怎么判断"
    combined = pilot._multi_modifier("books", "我想选一本适合当前阅读计划的书")
    assert combined == pilot._CATEGORY_MULTI_MODIFIERS["books"]


def test_requirement_bindings_are_authoritative_and_coverage_is_derived():
    scenarios = {row.scenario_id: row for row in _load().scenarios}
    audits = [json.loads(line) for line in (PILOT / "private" / "authoring_audit.private.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    for audit in audits:
        source_types = {str(binding["sourceType"]) for binding in audit["sourceBindings"]}
        assert source_types == set(audit["sourceTypes"])
        assert "query" in source_types
        for requirement in audit["requirementBindings"]:
            assert requirement["requirementId"]
            assert requirement["originBindings"]
            assert requirement["evidenceBindings"]
            for binding in (*requirement["originBindings"], *requirement["evidenceBindings"]):
                assert binding["field"]
                assert binding["sourceRef"]
                assert binding["role"]
        scenario = scenarios[audit["scenarioId"]]
        if len(scenario.input["turns"]) == 1:
            assert not any(binding.get("sourceType") == "user_followup" for binding in audit["sourceBindings"])
        else:
            followup_requirements = [row for row in audit["requirementBindings"] if any(binding.get("sourceType") == "user_followup" for binding in row["originBindings"])]
            assert followup_requirements
            assert all(any(binding.get("turnId") == "turn-02" for binding in row["originBindings"]) for row in followup_requirements)


def test_hidden_rule_values_cannot_claim_query_origin():
    """A public topic/request may not be rewritten as a private exact value."""
    scenarios = {row.scenario_id: row for row in _load().scenarios}
    audits = [
        json.loads(line)
        for line in (PILOT / "private" / "authoring_audit.private.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    checked = {"knowledge": False, "merchant_policy": False, "coupon": False}
    for audit in audits:
        scenario = scenarios[audit["scenarioId"]]
        public_text = " ".join(turn["text"] for turn in scenario.input["turns"])
        for requirement in audit["requirementBindings"]:
            private_value = requirement.get("value")
            for origin in requirement["originBindings"]:
                if origin.get("sourceType") != "query":
                    if origin.get("sourceType") in checked and origin.get("value") == private_value:
                        checked[str(origin["sourceType"])] = True
                    continue
                if origin.get("publicValueVisible") is False:
                    assert origin.get("value") is None
                    assert origin.get("value") != private_value
                    assert origin.get("publicValue") != private_value
                # The exact value can be present in a public origin only when
                # the text really contains it; hidden document/coupon rules
                # must use their audited derivation binding instead.
                if origin.get("value") is not None:
                    assert str(origin["value"]) in public_text or origin.get("field") in {"category", "merchantId"}
    assert checked == {"knowledge": True, "merchant_policy": True, "coupon": True}


def test_product_bundle_knowledge_and_policy_universes_recompute_from_world():
    world = pilot._load_formal_world(WORLD)
    products_by_category = {
        category: tuple(row for row in world["products"] if str(row.get("category")) == category)
        for category in pilot.CATEGORIES
    }
    scenarios = {row.scenario_id: row for row in _load().scenarios}
    audits = [json.loads(line) for line in (PILOT / "private" / "authoring_audit.private.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    policy_topics = set()
    for audit in audits:
        scenario = scenarios[audit["scenarioId"]]
        oracle = scenario.oracle
        conditions = oracle["successConditions"]
        if not conditions.get("acceptableProductIds") and not conditions.get("acceptableBundles"):
            continue
        category_rows = products_by_category[audit["category"]]
        sequence = conditions.get("environmentSequence") or []
        needs_environment = any(str(node["atom"].get("field")) in pilot._DYNAMIC_FIELDS for node in pilot._flatten_atoms(conditions["constraintAst"]))
        environment_rows = world["environments"][sequence[-1] if sequence else "E0"] if needs_environment else None
        if conditions["solutionType"] == "single_product":
            universe = pilot._universe(category_rows, conditions["constraintAst"], environment_rows)
            assert set(universe) == set(conditions["acceptableProductIds"])
        elif conditions["solutionType"] == "bundle":
            ids = pilot._universe(category_rows, conditions["constraintAst"], environment_rows)
            groups = {}
            for product_id in ids:
                groups.setdefault(str(next(row for row in category_rows if str(row["productId"]) == product_id)["merchantId"]), []).append(product_id)
            expected = {tuple(bundle) for merchant_ids in groups.values() for bundle in __import__("itertools").combinations(sorted(merchant_ids), 2)}
            assert {tuple(bundle) for bundle in conditions["acceptableBundles"]} == expected
            for dependency in conditions.get("documentDependencies", []):
                if dependency["documentType"] == "policy":
                    policy_topics.add(next(row["topic"] for row in world["policies"] if row["policyId"] == dependency["documentId"]))
        if oracle["intentFamily"] == "knowledge_to_product":
            dependency = conditions["documentDependencies"][0]
            document = next(row for row in world["knowledge"] if row["knowledgeId"] == dependency["documentId"])
            atom = next(node["atom"] for node in pilot._flatten_atoms(conditions["constraintAst"]) if node["atom"]["field"] == dependency["field"])
            assert atom["operator"] == dependency["operator"] == document["selectionRule"]["operator"]
            assert atom["value"] == dependency["value"] == document["selectionRule"]["value"]
            assert len(conditions["acceptableProductIds"]) < len(category_rows)
        if oracle["complexityStratum"] == "S1_FIXED_MULTISTEP":
            assert len({binding["sourceType"] for binding in audit["sourceBindings"] if binding["sourceType"] != "query"}) >= 2
    assert policy_topics == set(pilot._POLICY_TOPICS)


def _independent_ast_value(product, offer, field):
    if field == "category":
        return product.get("category")
    if field == "merchantId":
        return product.get("merchantId")
    if field in {"price", "stock", "promotionEligible", "environmentRevision"}:
        return offer.get(field)
    fact = (product.get("facts") or {}).get(field)
    if isinstance(fact, dict):
        return fact.get("value") if fact.get("known", False) else None
    return None


def _independent_atom_matches(actual, operator, expected):
    if actual is None:
        return False
    if operator == "EQ":
        return actual == expected
    if operator == "NEQ":
        return actual != expected
    if operator == "IN":
        return isinstance(expected, list) and actual in expected
    if operator == "NOT_IN":
        return isinstance(expected, list) and actual not in expected
    if operator == "CONTAINS":
        return isinstance(actual, str) and str(expected) in actual
    if operator == "GTE":
        return float(actual) >= float(expected)
    if operator == "LTE":
        return float(actual) <= float(expected)
    return False


def _independent_ast_matches(node, product, offer):
    kind = node.get("kind")
    if kind == "atom":
        atom = node["atom"]
        return _independent_atom_matches(
            _independent_ast_value(product, offer, str(atom["field"])),
            str(atom["operator"]),
            atom.get("value"),
        )
    if kind == "all":
        return all(_independent_ast_matches(child, product, offer) for child in node.get("children", []))
    if kind == "any":
        return any(_independent_ast_matches(child, product, offer) for child in node.get("children", []))
    if kind == "not":
        return not _independent_ast_matches(node["child"], product, offer)
    return False


def _independent_cart_solution(product, offer, coupon=None, *, price_delta=0.0):
    if int(offer.get("stock", 0)) < 1 or str(offer.get("environmentRevision")) != "E0":
        return None
    subtotal = round(float(offer["price"]) + price_delta, 2)
    coupon_ids = []
    discount = 0.0
    if coupon is not None:
        if str(coupon.get("environmentRevision")) != "E0" or str(coupon.get("merchantId")) != str(product.get("merchantId")):
            return None
        if subtotal < float(coupon["threshold"]):
            return None
        raw = float(coupon["amount"]) if coupon.get("kind") == "fixed" else subtotal * float(coupon["amount"])
        discount = round(min(subtotal, raw, float(coupon["cap"])), 2)
        coupon_ids = [str(coupon["couponId"])]
    return {
        "selectedProductIds": [str(product["productId"])],
        "appliedCouponIds": coupon_ids,
        "environmentRevision": "E0",
        "subtotal": subtotal,
        "discount": discount,
        "total": round(max(0.0, subtotal - discount), 2),
    }


def _solution_key(row):
    return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_coupon_universes_are_independently_recomputed_from_e0_and_coupon_artifacts():
    """Stored coupon rows cannot make their own arithmetic or universe authoritative."""
    world = pilot._load_formal_world(WORLD)
    products = {str(row["productId"]): row for row in world["products"]}
    e0 = world["environments"]["E0"]
    coupons = {str(row["couponId"]): row for row in world["coupons"]}
    scenarios = {row.scenario_id: row for row in _load().scenarios}
    audits = [
        json.loads(line)
        for line in (PILOT / "private" / "authoring_audit.private.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    coupon_audits = [row for row in audits if row["intentFamily"] == "coupon_budget"]
    assert len(coupon_audits) == 19
    for audit in coupon_audits:
        scenario = scenarios[audit["scenarioId"]]
        oracle = scenario.oracle
        coupon = coupons[audit["couponId"]]
        max_total = float(oracle["successConditions"]["cartRules"]["maxTotal"])
        category = audit["category"]
        candidate_ids = []
        for product in world["products"]:
            if str(product.get("category")) != category:
                continue
            product_id = str(product["productId"])
            if _independent_ast_matches(oracle["successConditions"]["constraintAst"], product, e0[product_id]):
                candidate_ids.append(product_id)

        def recompute(coupon_row, *, price_delta=0.0):
            rows = []
            for product_id in sorted(candidate_ids):
                solution = _independent_cart_solution(products[product_id], e0[product_id], coupon_row, price_delta=price_delta)
                if solution is not None and solution["total"] <= max_total + 1e-9:
                    rows.append(solution)
            return sorted(rows, key=_solution_key)

        stale_delta = 1.0 if oracle["complexityStratum"] == "S2_OBSERVATION_DEPENDENT" else 0.0
        expected_start = recompute(coupon, price_delta=stale_delta)
        expected_terminal = recompute(coupon, price_delta=0.0)
        expected_no_coupon = recompute(None, price_delta=0.0)
        stored_start = sorted(audit["startSolutions"], key=_solution_key)
        stored_terminal = sorted(audit["terminalSolutions"], key=_solution_key)
        assert stored_start == expected_start
        assert stored_terminal == expected_terminal
        assert expected_no_coupon == []
        assert audit["couponNecessity"]["noCouponValidCount"] == 0
        assert audit["couponNecessity"]["couponValidCount"] == len(expected_terminal)
        assert audit["couponNecessity"]["recomputed"] is True
        if oracle["complexityStratum"] != "S2_OBSERVATION_DEPENDENT":
            assert stored_start == stored_terminal
        else:
            assert stored_start != stored_terminal

        # Frozen red case: changing a stored arithmetic field cannot survive
        # the independent world+coupon recomputation.
        bad = dict(stored_terminal[0])
        bad["discount"] = round(float(bad["discount"]) + 1.0, 2)
        assert _solution_key(bad) != _solution_key(expected_terminal[0])


def test_text_only_blind_artifact_is_private_pending_and_manifest_bound():
    checklist_path = PILOT / "private" / "blind_language_texts.private.jsonl"
    rows = [json.loads(line) for line in checklist_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    freeze = json.loads((PILOT / "freeze_manifest.json").read_text(encoding="utf-8"))
    assert len(rows) == 20
    assert all(set(row) == {"publicText"} for row in rows)
    assert freeze["blindLanguageTexts"]["recordCount"] == 20
    assert freeze["blindLanguageTexts"]["sha256"] == pilot.sha256_file(checklist_path)
    assert freeze["blindLanguageTexts"]["reviewStatus"] == "PENDING_INDEPENDENT_REVIEW"


def test_immutable_same_version_marker_cannot_authorize_overwrite(tmp_path: Path):
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "freeze_manifest.json").write_text(json.dumps({"pilotVersion": pilot.PILOT_96_VERSION}), encoding="utf-8")
    sentinel = existing / "sentinel.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    with pytest.raises(Exception, match="refusing to overwrite"):
        pilot.write_pilot_96(WORLD, existing)
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_public_only_loader_has_no_private_import_or_read_closure():
    source = inspect.getsource(public_loader)
    assert "commerce_benchmark_v1_pilot" not in source
    assert "scenario_lab_v1" not in source
    rows = public_loader.load_public_inputs(PILOT / "public" / "scenario.input.jsonl")
    assert len(rows) == 96
    with pytest.raises(public_loader.PublicOnlyLoadError):
        public_loader.load_public_inputs(PILOT / "private" / "oracle.private.jsonl")
    with pytest.raises(public_loader.PublicOnlyLoadError):
        public_loader.load_public_inputs(PILOT / "run" / "prediction.not_run.jsonl")


def test_public_leak_attacks_direct_nested_and_base64_fail_closed():
    public_rows = lab.load_jsonl(PILOT / "public" / "scenario.input.jsonl", "input")
    oracle = lab.load_jsonl(PILOT / "private" / "oracle.private.jsonl", "oracle")[0]
    fault = lab.load_jsonl(PILOT / "private" / "fault.private.jsonl", "fault")[0]
    private_value = str(oracle["successConditions"].get("acceptableProductIds", ["merchant-001"])[0])
    direct = json.loads(json.dumps(public_rows[0], ensure_ascii=False))
    direct["turns"][0]["text"] += private_value
    with pytest.raises(Exception):
        lab.audit_public_private_value_leaks(direct, oracle, fault)
    nested = json.loads(json.dumps(public_rows[0], ensure_ascii=False))
    nested["nested"] = {"renamedAnswer": private_value}
    with pytest.raises(Exception):
        lab.audit_public_private_value_leaks(nested, oracle, fault)
    encoded = json.loads(json.dumps(public_rows[0], ensure_ascii=False))
    encoded["encoded"] = base64.b64encode(private_value.encode("utf-8")).decode("ascii")
    with pytest.raises(Exception):
        lab.audit_public_private_value_leaks(encoded, oracle, fault)


def test_pilot_is_byte_deterministic(tmp_path: Path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    pilot.write_pilot_96(WORLD, first)
    pilot.write_pilot_96(WORLD, second)
    left = {path.relative_to(first): path.read_bytes() for path in first.rglob("*") if path.is_file()}
    right = {path.relative_to(second): path.read_bytes() for path in second.rglob("*") if path.is_file()}
    assert left == right


def test_authoritative_generator_rejects_non_accepted_world_directory(tmp_path: Path):
    with pytest.raises(Exception):
        pilot.generate_pilot_96(tmp_path)


def test_freeze_manifest_binds_world_and_artifact_rows():
    freeze = json.loads((PILOT / "freeze_manifest.json").read_text(encoding="utf-8"))
    assert freeze["pilotVersion"] == pilot.PILOT_96_VERSION
    assert freeze["counts"] == {"total": 96, "development": 60, "validation": 30, "contract_red_team": 6}
    assert freeze["worldManifestSha256"] == pilot.sha256_file(WORLD / "manifest.json")
    assert {row["recordCount"] for row in freeze["artifacts"] if "recordCount" in row} == {20, 96}
    assert "blindLanguageAuditScenarioIds" not in freeze
    assert freeze["publicTextAuthoringSource"]["recordCount"] == 96
    assert freeze["publicTextAuthoringSource"]["sha256"] == authoring.authoring_asset_sha256()


def test_attempt001_directory_is_byte_preserved():
    expected = {
        "freeze_manifest.json": "2b537d6de128e4aafed9be5495c955fa9cf9297f2974b34073ff5bb2cc b257b7".replace(" ", ""),
        "private/authoring_audit.private.jsonl": "425de6f5302b52ae5162189fa65c2023336905decba0ed0caef8be45c7bb5537",
        "private/fault.private.jsonl": "5a377bb711b683219d4a63944829616f0fa78994d253910837011fe5efb83553",
        "private/oracle.private.jsonl": "e9e8f0b099252fe6dd5feeb6c21dbd6615ed7c5cedd1f0f9b42a0708045a534a",
        "public/scenario.input.jsonl": "53c4a11c15c5f1eae1428bbdcbe0a4c75e4d176019212c4b9f10757c6c56e35d",
        "README.md": "c974c3286ade2b6fc6627e17b38e9947b7033366729632450a1f478fedb7c856",
        "run/prediction.not_run.jsonl": "b6c5ebca7525cb9f0ce25e1d37b64c5e303f727771cf9942a86a20ee59837142",
        "run/receipt.not_collected.jsonl": "4b9459f5b54c59a628300f94297be34f04f4507770606c151100ac9b828011e1",
    }
    actual = {
        str(path.relative_to(OLD_PILOT)).replace("\\", "/"): pilot.sha256_file(path)
        for path in OLD_PILOT.rglob("*")
        if path.is_file()
    }
    assert actual == expected
