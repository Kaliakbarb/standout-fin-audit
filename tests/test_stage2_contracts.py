"""Independent financial examples and versioned storage/schema contracts for G2.

These checks are local. They do not stand in for the final 20+20 evaluation or
the final 10+10 train/dev release artifacts.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, validators

from finance_examples import boundary_example as _boundary_example, fx_example as _fx_example, update_counts as _counts

from fin_audit.checker import reconcile
from fin_audit.contracts import SCHEMA_VERSION, EpisodeBundle, canonical_json, evidence_registry
from fin_audit.evaluation import evaluate
from fin_audit.generator import generate
from fin_audit.profiles import stage2_cases
from fin_audit.runner import oracle_action
from fin_audit.storage import FX_COLUMNS, LEDGER_COLUMNS, StorageError, export_episode, load_episode


SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "schemas" / "episode.schema.json").read_text()
)
StrictValidator = validators.extend(
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
        "integer", lambda checker, value: type(value) is int
    ),
)


def _validator(definition=None):
    schema = SCHEMA if definition is None else {
        "$schema": SCHEMA["$schema"], "$defs": SCHEMA["$defs"],
        "$ref": f"#/$defs/{definition}",
    }
    return StrictValidator(schema, format_checker=FormatChecker())


def _bundle(public):
    return EpisodeBundle(public, reconcile(public), {
        "schema_version": public["manifest"]["schema_version"],
        "public_sha256": hashlib.sha256(canonical_json(public).encode()).hexdigest(),
        "fixture": "independently specified financial example",
    })


@pytest.mark.parametrize("case", list(stage2_cases()))
def test_real_stage_two_profiles_validate_and_roundtrip(tmp_path, case):
    bundle = generate(71, stage2_cases()[case])
    _validator().validate(bundle.public)
    for issue in bundle.truth:
        _validator("truth_issue").validate(issue)
    export_episode(bundle, tmp_path / case)
    restored = load_episode(tmp_path / case)
    assert restored == bundle
    assert any(row["source_currency"] != "KZT" for row in restored.public["ledger"])
    assert any(doc["total_minor"] < 0 for doc in restored.public["invoices"])
    with (tmp_path / case / "public" / "fx.csv").open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames[:4] == list(FX_COLUMNS)
        assert "record_id" in reader.fieldnames
        assert all(type(row["rate"]) is str for row in restored.public["fx"])
    with (tmp_path / case / "public" / "ledger.csv").open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames[:9] == list(LEDGER_COLUMNS)
        rows = list(reader)
    assert all(type(json.loads(row["source_amount_minor"])) is int for row in rows)
    assert any(json.loads(row["source_amount_minor"]) < 0 for row in rows)
    assert any(json.loads(row["allocated_minor"]) is None for row in rows)


def test_native_v01_and_v02_remain_schema_and_storage_compatible(tmp_path):
    generated = generate(72)
    for version in ("0.1.0", "0.2.0"):
        public = copy.deepcopy(generated.public)
        public["manifest"]["schema_version"] = version
        public["policy"]["schema_version"] = version
        bundle = _bundle(public)
        _validator().validate(public)
        export_episode(bundle, tmp_path / version)
        assert load_episode(tmp_path / version) == bundle
    mixed = copy.deepcopy(generated.public)
    mixed["manifest"]["schema_version"] = "0.1.0"
    mixed["policy"]["schema_version"] = "0.2.0"
    assert not _validator().is_valid(mixed)


def test_hand_checked_allocation_and_fx_effects_change_after_conditional_repair():
    observed = _fx_example()
    _validator().validate(observed)
    issues = reconcile(observed)
    assert {issue["type"]: issue["expected_amount_minor"] for issue in issues} == {
        "payment_allocation": 4_950_000, "fx_conversion": 600_000,
    }
    assert sum(row["debit_minor"] for row in observed["ledger"]) == sum(row["credit_minor"] for row in observed["ledger"])
    corrected = _fx_example(corrected=True)
    assert [(issue["type"], issue["expected_amount_minor"]) for issue in reconcile(corrected)] == [
        ("payment_allocation", 5_050_000)
    ]
    assert reconcile(_fx_example(corrected=True, fully_allocated=True)) == []


def test_foreign_monetary_values_beyond_float_and_64bit_ranges_roundtrip_exactly(tmp_path):
    public = _fx_example()
    factor = 2**60 + 7
    for invoice in public["invoices"]:
        invoice["total_minor"] *= factor
        for line in invoice["line_items"]:
            line["unit_price_minor"] *= factor
            line["total_minor"] *= factor
    for payment in public["payments"]:
        payment["amount_minor"] *= factor
    for row in public["ledger"]:
        for field in ("debit_minor", "credit_minor", "source_amount_minor"):
            row[field] *= factor
        if row["allocated_minor"] is not None:
            row["allocated_minor"] *= factor
    bundle = _bundle(public)
    expected = {"payment_allocation": 4_950_000 * factor, "fx_conversion": 600_000 * factor}
    assert {issue["type"]: issue["expected_amount_minor"] for issue in bundle.truth} == expected
    _validator().validate(public)
    export_episode(bundle, tmp_path / "big-integers")
    assert load_episode(tmp_path / "big-integers") == bundle
    payload = oracle_action(bundle)["args"]["findings"]
    result = evaluate(payload, bundle.truth, evidence_registry(public), 0, public["manifest"]["episode_id"])
    assert result.tp == 2
    payload["findings"][0]["claimed_amount_minor"] += 1
    result = evaluate(payload, bundle.truth, evidence_registry(public), 0, public["manifest"]["episode_id"])
    assert (result.tp, result.fp, result.fn) == (1, 1, 1)


@pytest.mark.parametrize("source,recorded", [
    ("2026-12-01T00:00:00+05:00", "2026-11-30T23:59:59+05:00"),
    ("2026-11-30T23:59:59+05:00", "2026-12-01T00:00:00+05:00"),
    ("2026-11-01T00:00:00+05:00", "2026-10-31T23:59:59+05:00"),
    ("2026-10-31T23:59:59+05:00", "2026-11-01T00:00:00+05:00"),
])
def test_cutoff_uses_half_open_period_membership_in_both_directions(source, recorded):
    public = _boundary_example(source, recorded)
    _validator().validate(public)
    issues = reconcile(public)
    assert len(issues) == 1
    assert issues[0]["type"] == "period_cutoff"
    assert issues[0]["affected_ids"] == ["primary"]
    assert issues[0]["expected_amount_minor"] == 100_000


@pytest.mark.parametrize("source,recorded", [
    ("2026-12-01T00:00:00+05:00", "2026-11-30T19:00:00+00:00"),
    ("2026-11-01T00:00:00+05:00", "2026-10-31T19:00:00+00:00"),
])
def test_same_instant_with_different_offset_and_calendar_date_is_clean(source, recorded):
    public = _boundary_example(source, recorded)
    _validator().validate(public)
    assert reconcile(public) == []


@pytest.mark.parametrize("negative_payment", [False, True])
def test_negative_credit_note_and_refund_primary_support_cutoff(negative_payment):
    public = copy.deepcopy(generate(73, stage2_cases()["clean_multicurrency"]).public)
    credit_note = next(doc for doc in public["invoices"] if doc["total_minor"] < 0)
    if negative_payment:
        doc = next(doc for doc in public["payments"] if doc["amount_minor"] < 0)
        rows = [row for row in public["ledger"] if row["payment_id"] == doc["payment_id"]]
        account = public["policy"]["accounts"]["cash"]
        amount = -doc["amount_minor"]
    else:
        rows = [row for row in public["ledger"] if row["invoice_id"] == credit_note["invoice_id"] and row["payment_id"] is None]
        account = public["policy"]["accounts"]["receivable"]
        amount = -credit_note["total_minor"]
    for row in rows:
        row["timestamp"] = "2026-12-02T00:00:00+05:00"
    primary = next(row for row in rows if row["account"] == account)
    assert primary["credit_minor"] == amount
    issues = reconcile(public)
    assert len(issues) == 1
    assert (issues[0]["type"], issues[0]["affected_ids"], issues[0]["expected_amount_minor"]) == (
        "period_cutoff", [primary["row_id"]], amount
    )


@pytest.mark.parametrize("rate", ["NaN", "Infinity", "0", "-1", 505.0])
def test_invalid_fx_table_rate_is_rejected_without_private_truth(rate):
    public = _fx_example()
    public["fx"][0]["rate"] = rate
    with pytest.raises(ValueError):
        reconcile(public)
    assert not _validator().is_valid(public)


def test_missing_normative_fx_and_wrong_source_currency_are_rejected():
    missing_rate = _fx_example()
    missing_rate["fx"] = [row for row in missing_rate["fx"] if row["record_id"] != "payment-rate"]
    _counts(missing_rate)
    with pytest.raises(ValueError):
        reconcile(missing_rate)
    wrong_currency = _fx_example()
    for row in wrong_currency["ledger"]:
        if row["payment_id"] is not None:
            row["source_currency"] = "KZT"
    with pytest.raises(ValueError):
        reconcile(wrong_currency)


def test_complete_fx_proof_currency_and_tolerance_survive_stage_two(tmp_path):
    bundle = _bundle(_fx_example())
    payload = oracle_action(bundle)["args"]["findings"]
    registry = evidence_registry(bundle.public)
    kwargs = dict(truth=bundle.truth, registry=registry, inspection_steps=0,
                  episode_id=bundle.public["manifest"]["episode_id"])
    assert evaluate(payload, **kwargs).tp == 2
    index = next(index for index, claim in enumerate(payload["findings"]) if claim["type"] == "fx_conversion")
    fx = payload["findings"][index]
    assert "payment-rate" in fx["evidence_ids"]
    wrong_currency = copy.deepcopy(payload)
    wrong_currency["findings"][index]["currency"] = "USD"
    assert evaluate(wrong_currency, **kwargs).tp == 1
    incomplete = copy.deepcopy(payload)
    incomplete["findings"][index]["evidence_ids"].remove("payment-rate")
    assert evaluate(incomplete, **kwargs).tp == 1
    near = copy.deepcopy(payload)
    near["findings"][index]["claimed_amount_minor"] += 1
    assert evaluate(near, **kwargs).tp == 1
    assert evaluate(near, **kwargs, tolerances={"fx_conversion": 1}).tp == 2
    broken_truth = copy.deepcopy(bundle.truth)
    broken_truth[index]["evidence_sets"][0].remove("payment-rate")
    with pytest.raises(StorageError, match="truth_does_not_match"):
        export_episode(EpisodeBundle(bundle.public, broken_truth, bundle.provenance), tmp_path / "bad-proof")


def test_serialized_fx_corruption_and_metadata_loss_fail_closed(tmp_path):
    bundle = _bundle(_fx_example())
    export_episode(bundle, tmp_path / "fx-corrupt")
    path = tmp_path / "fx-corrupt" / "public" / "fx.csv"
    path.write_text(path.read_text().replace(",505,", ",NaN,", 1), encoding="utf-8")
    with pytest.raises(StorageError):
        load_episode(tmp_path / "fx-corrupt")
    export_episode(bundle, tmp_path / "metadata-corrupt")
    path = tmp_path / "metadata-corrupt" / "public" / "ledger.csv"
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fields, rows = reader.fieldnames, list(reader)
    rows[0]["source_currency"] = json.dumps("KZT")
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(StorageError):
        load_episode(tmp_path / "metadata-corrupt")
