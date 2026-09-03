"""Materialize the approved G2 Phase A A/B adjudication as a separate frozen bundle.

This deliberately reads only the two returned reviewer workbooks, the approved AI
proposal and the public Phase-A manifest.  It never reads a private oracle/fault
source and never mutates a reviewer workbook or an earlier freeze.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = ROOT / "data" / "annotations" / "ecommerce" / "kuaisearch_multicategory_retrieval_g2_phase_a_adjudicated_20260824_r2"
REVIEWER_A = Path(r"C:\Users\ming\Downloads\KuaiSearch_G2_Reviewer_A_已填写.xlsx")
REVIEWER_B = Path(r"C:\Users\ming\Downloads\3363b781-7540-49f3-8c82-5742e3663107_filled.xlsx")
PROPOSAL = ROOT / "outputs" / "01a02842-36cb-7bb2-8fb2-46a897266376" / "kuaisearch-g2-a-b-disagreement-ai-proposal-20260824.xlsx"
PHASE_A_MANIFEST = ROOT / "data" / "annotations" / "ecommerce" / "kuaisearch_multicategory_retrieval_g2_phase_a_20260824_r1" / "manifest.json"
PUBLIC_SAMPLE = PHASE_A_MANIFEST.parent / "public_review_sample.jsonl"

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main", "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
GRADE = re.compile(r"^[0-3]")
QUERY_ID = re.compile(r"^ksq-[0-9a-f]{24}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
CONFLICT_VALUES = {"是", "否"}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _xlsx_sheet_rows(path: Path, index: int) -> list[dict[str, str]]:
    """Read cell values from one worksheet using stdlib only (no Excel recalc)."""
    with zipfile.ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(node.itertext()) for node in root.findall("m:si", NS)]
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        rels = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {node.get("Id"): node.get("Target", "").lstrip("/") for node in rels}
        sheet = workbook.findall("m:sheets/m:sheet", NS)[index]
        target = targets[sheet.get("{" + NS["r"] + "}id")]
        if not target.startswith("xl/"):
            target = "xl/" + target
        root = ElementTree.fromstring(archive.read(target))
    rows: list[dict[str, str]] = []
    for row in root.findall(".//m:sheetData/m:row", NS):
        values: dict[str, str] = {}
        for cell in row.findall("m:c", NS):
            value = cell.find("m:v", NS)
            text = "" if value is None or value.text is None else value.text
            if cell.get("t") == "s" and text:
                text = shared[int(text)]
            elif cell.get("t") == "inlineStr":
                text = "".join(cell.itertext())
            column = "".join(character for character in cell.get("r", "") if character.isalpha())
            values[column] = text
        if values:
            rows.append(values)
    return rows


def normalize_grade(value: str) -> str:
    value = value.strip().upper()
    if value.startswith("U"):
        return "U"
    match = GRADE.match(value)
    if not match:
        raise ValueError(f"unrecognized reviewer grade: {value!r}")
    return match.group(0)


def reviewer_key(row: dict[str, str]) -> tuple[str, str, str, str, str]:
    return tuple(row.get(column, "") for column in "BCDEF")  # query/title/attrs/brand/seller


def proposal_key(row: dict[str, str]) -> tuple[str, str, str]:
    return tuple(row.get(column, "") for column in "CDE")  # query/title/attrs


def _source(path: Path, *, role: str, rows: int | None = None) -> dict[str, object]:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"source must be a regular non-symlink file: {path}")
    try:
        relative_path = path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        # Test bundles may be materialized outside the workspace; never emit an absolute path.
        relative_path = path.name
    result: dict[str, object] = {"path": relative_path, "sha256": sha256(path), "bytes": path.stat().st_size, "role": role}
    if rows is not None:
        result["rows"] = rows
    return result


def _load_frozen_public_sample(public_sample: Path = PUBLIC_SAMPLE) -> tuple[dict[str, object], list[dict[str, object]]]:
    phase_manifest = json.loads(PHASE_A_MANIFEST.read_text(encoding="utf-8"))
    declared = phase_manifest["artifacts"]["publicReviewSample"]
    if (
        declared["rows"] != 508
        or declared["bytes"] != public_sample.stat().st_size
        or declared["sha256"] != sha256(public_sample)
    ):
        raise ValueError("public sample does not match the frozen Phase A manifest")
    sample_rows = [json.loads(line) for line in public_sample.read_text(encoding="utf-8").splitlines()]
    if len(sample_rows) != declared["rows"]:
        raise ValueError("public sample row count does not match the frozen Phase A manifest")
    if any(
        not QUERY_ID.fullmatch(str(row.get("queryId", "")))
        or not HEX64.fullmatch(str(row.get("blindCandidateId", "")))
        for row in sample_rows
    ):
        raise ValueError("public sample contains an invalid query or candidate identity")
    return phase_manifest, sample_rows


def build_records(reviewer_a: Path, reviewer_b: Path, proposal: Path, public_sample: Path = PUBLIC_SAMPLE) -> tuple[list[dict[str, object]], dict[str, int]]:
    a_rows, b_rows, proposal_rows = (_xlsx_sheet_rows(reviewer_a, 2)[1:], _xlsx_sheet_rows(reviewer_b, 2)[1:], _xlsx_sheet_rows(proposal, 2)[1:])
    if len(a_rows) != 556 or len(b_rows) != 124 or len(proposal_rows) != 42:
        raise ValueError("unexpected workbook row counts")
    if any(row.get("H") not in CONFLICT_VALUES for row in a_rows + b_rows):
        raise ValueError("reviewer hard-constraint conflict is outside the allowed domain")
    a_groups: dict[tuple[str, str, str, str, str], list[dict[str, str]]] = {}
    for row in a_rows:
        a_groups.setdefault(reviewer_key(row), []).append(row)
    a_by_key = {key: rows[0] for key, rows in a_groups.items()}
    b_by_key = {reviewer_key(row): row for row in b_rows}
    # Reviewer A has 48 blinded repeats (556 rows / 508 unique candidates); never silently discard them.
    # The B lane contains only the 124 unique second-review candidates.
    if len(a_by_key) != 508 or len(b_by_key) != 124 or set(b_by_key) - set(a_by_key):
        raise ValueError("reviewer identity coverage is not one-to-one")
    for rows in a_groups.values():
        if len({(normalize_grade(row["G"]), row["H"]) for row in rows}) != 1:
            raise ValueError("Reviewer A blinded repeat score/conflict mismatch")
    _, sample_rows = _load_frozen_public_sample(public_sample)
    sample = {tuple(row[field] for field in ("query", "title", "attr_value", "brand", "seller_name")): row for row in sample_rows}
    if len(sample_rows) != 508 or len(sample) != 508:
        raise ValueError("public sample identity coverage drift")
    proposed = {proposal_key(row): (normalize_grade(row["N"]), row["O"]) for row in proposal_rows}
    if len(proposed) != 42:
        raise ValueError("proposal identities are not unique")
    if any(conflict not in CONFLICT_VALUES for _, conflict in proposed.values()):
        raise ValueError("proposal hard-constraint conflict is outside the allowed domain")
    records: list[dict[str, object]] = []
    disagreements = 0
    for key in sorted(b_by_key):
        a, b = a_by_key[key], b_by_key[key]
        a_grade, b_grade = normalize_grade(a["G"]), normalize_grade(b["G"])
        identity = proposal_key({"C": key[0], "D": key[1], "E": key[2]})
        if a_grade == b_grade:
            if a["H"] != b["H"]:
                raise ValueError("agreement has A/B hard-constraint conflict mismatch")
            adjudicated, final_conflict, source = a_grade, a["H"], "REVIEWER_A_B_AGREEMENT"
        else:
            disagreements += 1
            if identity not in proposed:
                raise ValueError("A/B disagreement missing from approved AI proposal")
            adjudicated, final_conflict = proposed.pop(identity)
            source = "USER_APPROVED_AI_PROPOSAL"
        if key not in sample:
            raise ValueError("paired review identity is absent from public review sample")
        sample_row = sample[key]
        records.append({
            "schemaVersion": "kuaisearch-g2-phase-a-adjudicated-qrel-v1",
            "queryId": sample_row["queryId"], "blindCandidateId": sample_row["blindCandidateId"],
            "reviewerAItemIds": [row["A"] for row in a_groups[key]], "secondReviewItemId": b["A"],
            "relevance": adjudicated, "hardConstraintConflict": final_conflict,
            "resolutionSource": source, "reviewerAGrade": a_grade, "reviewerBGrade": b_grade,
            "reviewerAConflict": a["H"], "reviewerBConflict": b["H"],
        })
    if proposed or disagreements != 42 or len(records) != 124:
        raise ValueError("proposal does not exactly bind the 42 A/B disagreements")
    counts = {"pairedRows": len(records), "queries": len({r["queryId"] for r in records}), "agreementRows": sum(r["resolutionSource"] == "REVIEWER_A_B_AGREEMENT" for r in records), "approvedAdjudicationRows": disagreements, "unknownRows": sum(r["relevance"] == "U" for r in records)}
    if counts["agreementRows"] != 82:
        raise ValueError(f"expected 82 agreements, got {counts['agreementRows']}")
    return records, counts


def materialize(output_dir: Path = DEFAULT_OUTPUT_DIR, reviewer_a: Path = REVIEWER_A, reviewer_b: Path = REVIEWER_B, proposal: Path = PROPOSAL) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = output_dir / "receipt.json"
    receipt_path.unlink(missing_ok=True)
    evidence_dir = output_dir / "source_evidence"
    evidence_dir.mkdir(exist_ok=True)
    snapshots = {"reviewer_a_return.xlsx": reviewer_a, "reviewer_b_return.xlsx": reviewer_b, "approved_ai_proposal.xlsx": proposal}
    for name, source in snapshots.items():
        target = evidence_dir / name
        if not target.exists():
            shutil.copyfile(source, target)
        if sha256(target) != sha256(source):
            raise ValueError(f"source evidence snapshot mismatch: {name}")
    reviewer_a, reviewer_b, proposal = (evidence_dir / "reviewer_a_return.xlsx", evidence_dir / "reviewer_b_return.xlsx", evidence_dir / "approved_ai_proposal.xlsx")
    records, counts = build_records(reviewer_a, reviewer_b, proposal)
    qrels = output_dir / "adjudicated_qrels.jsonl"
    qrels.write_text("".join(canonical(record) + "\n" for record in records), encoding="utf-8")
    evidence = {"schemaVersion": "kuaisearch-g2-human-adjudication-evidence-v1", "status": "USER_APPROVED_AI_PROPOSAL", "completedOn": "2026-08-24", "statement": "The user approved the AI adjudication proposal for the 42 A/B disagreements.", "notClaimed": "This is approval of a proposal, not a claim that the user filled each adjudication cell manually.", "inputs": [_source(reviewer_a, role="REVIEWER_A_RETURN_SNAPSHOT", rows=556), _source(reviewer_b, role="REVIEWER_B_RETURN_SNAPSHOT", rows=124), _source(proposal, role="AI_PROPOSAL_SNAPSHOT", rows=42)]}
    evidence_path = output_dir / "human_adjudication_evidence.json"
    evidence_path.write_text(canonical(evidence) + "\n", encoding="utf-8")
    phase_manifest, _ = _load_frozen_public_sample()
    manifest = {"schemaVersion": "kuaisearch-g2-phase-a-adjudicated-qrels-manifest-v1", "version": "kuaisearch-g2-phase-a-adjudicated-20260824-r2", "status": "PHASE_A_ADJUDICATED_QRELS_READY_ANALYSIS_PENDING", "coverageScope": "SECOND_REVIEW_TEST_LANE_ONLY", "claimBoundary": "NO_RETRIEVAL_WINNER_OR_ARCHITECTURE_BENEFIT_DECLARED", "counts": counts, "unknownHandling": "U is preserved as unknown and is never coerced to relevance 0.", "sourceBindings": {"phaseAManifest": _source(PHASE_A_MANIFEST, role="FROZEN_PHASE_A_MANIFEST"), "publicReviewSample": _source(PUBLIC_SAMPLE, role="PUBLIC_REVIEW_SAMPLE", rows=508), "reviewerA": evidence["inputs"][0], "reviewerB": evidence["inputs"][1], "approvedAiProposal": evidence["inputs"][2]}, "artifacts": {"adjudicatedQrels": _source(qrels, role="ADJUDICATED_QRELS", rows=124), "humanAdjudicationEvidence": _source(evidence_path, role="HUMAN_ADJUDICATION_EVIDENCE")}, "phaseAThresholdResult": "NOT_EVALUATED_BY_THIS_FREEZE", "remainingGates": ["Run the preregistered agreement, unknown-rate, coverage, residual, paired-CI, leave-one-system-out and pool-depth analyses.", "A passing Phase A analysis still does not itself declare a retrieval winner."]}
    manifest["frozenPhaseAReviewVersion"] = phase_manifest["reviewVersion"]
    manifest["canonicalDigest"] = hashlib.sha256(canonical(manifest).encode("utf-8")).hexdigest()
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(canonical(manifest) + "\n", encoding="utf-8")
    # No PASS receipt exists until the complete independent replay succeeds.
    _validate_core(output_dir)
    receipt = {"schemaVersion": "kuaisearch-g2-phase-a-adjudicated-qrels-receipt-v1", "status": manifest["status"], "counts": counts, "checks": {"pairedCoverage": "PASS", "approvedProposalCoverage": "PASS", "unknownPreserved": "PASS", "sourceBinding": "PASS", "scoreConflictReplay": "PASS"}, "manifest": _source(manifest_path, role="MANIFEST"), "qrels": _source(qrels, role="ADJUDICATED_QRELS", rows=124), "humanAdjudicationEvidence": _source(evidence_path, role="HUMAN_ADJUDICATION_EVIDENCE")}
    receipt_path.write_text(canonical(receipt) + "\n", encoding="utf-8")
    validate_bundle(output_dir)
    return output_dir


def _validate_core(output_dir: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    records = [json.loads(line) for line in (output_dir / "adjudicated_qrels.jsonl").read_text(encoding="utf-8").splitlines()]
    if len(records) != 124 or manifest["counts"] != {"pairedRows": 124, "queries": 12, "agreementRows": 82, "approvedAdjudicationRows": 42, "unknownRows": sum(r["relevance"] == "U" for r in records)}:
        raise ValueError("adjudicated coverage/count validation failed")
    if manifest.get("coverageScope") != "SECOND_REVIEW_TEST_LANE_ONLY":
        raise ValueError("adjudicated coverage scope validation failed")
    if any(r["relevance"] not in {"0", "1", "2", "3", "U"} for r in records) or any(r["relevance"] == "0" and r["reviewerAGrade"] == "U" and r["reviewerBGrade"] == "U" for r in records):
        raise ValueError("invalid grade or unknown coercion")
    if any(
        not QUERY_ID.fullmatch(str(r.get("queryId", "")))
        or not HEX64.fullmatch(str(r.get("blindCandidateId", "")))
        or r.get("hardConstraintConflict") not in CONFLICT_VALUES
        for r in records
    ):
        raise ValueError("invalid adjudicated identity or conflict domain")
    if sum(r["resolutionSource"] == "USER_APPROVED_AI_PROPOSAL" for r in records) != 42:
        raise ValueError("approved-adjudication coverage validation failed")
    if sum(r["resolutionSource"] == "REVIEWER_A_B_AGREEMENT" for r in records) != 82:
        raise ValueError("agreement coverage validation failed")
    evidence_dir = output_dir / "source_evidence"
    rebuilt, rebuilt_counts = build_records(evidence_dir / "reviewer_a_return.xlsx", evidence_dir / "reviewer_b_return.xlsx", evidence_dir / "approved_ai_proposal.xlsx")
    if rebuilt != records or rebuilt_counts != manifest["counts"]:
        raise ValueError("source binding, score, or conflict replay validation failed")
    for binding_name, path in (("phaseAManifest", PHASE_A_MANIFEST), ("publicReviewSample", PUBLIC_SAMPLE), ("reviewerA", evidence_dir / "reviewer_a_return.xlsx"), ("reviewerB", evidence_dir / "reviewer_b_return.xlsx"), ("approvedAiProposal", evidence_dir / "approved_ai_proposal.xlsx")):
        if manifest["sourceBindings"][binding_name]["sha256"] != sha256(path):
            raise ValueError(f"source hash drift: {binding_name}")
        if Path(str(manifest["sourceBindings"][binding_name]["path"])).is_absolute():
            raise ValueError(f"absolute source path is not portable: {binding_name}")
    for artifact_name, path in (("adjudicatedQrels", output_dir / "adjudicated_qrels.jsonl"), ("humanAdjudicationEvidence", output_dir / "human_adjudication_evidence.json")):
        if manifest["artifacts"][artifact_name]["sha256"] != sha256(path):
            raise ValueError(f"artifact hash drift: {artifact_name}")
    expected = dict(manifest); digest = expected.pop("canonicalDigest")
    if digest != hashlib.sha256(canonical(expected).encode("utf-8")).hexdigest():
        raise ValueError("manifest canonical digest mismatch")
    return manifest, records


def validate_bundle(output_dir: Path = DEFAULT_OUTPUT_DIR) -> None:
    _validate_core(output_dir)
    receipt = json.loads((output_dir / "receipt.json").read_text(encoding="utf-8"))
    if receipt["manifest"]["sha256"] != sha256(output_dir / "manifest.json") or receipt["qrels"]["sha256"] != sha256(output_dir / "adjudicated_qrels.jsonl") or receipt["checks"] != {"pairedCoverage": "PASS", "approvedProposalCoverage": "PASS", "unknownPreserved": "PASS", "sourceBinding": "PASS", "scoreConflictReplay": "PASS"}:
        raise ValueError("receipt binding validation failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    materialize(args.output_dir)
    validate_bundle(args.output_dir)
