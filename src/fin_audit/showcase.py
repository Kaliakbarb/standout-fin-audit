"""Hand-checkable, post-run demonstrations of financial and scoring semantics.

These are openly documented demonstration fixtures, not hidden benchmark data.
Repairs consume only public documents, policy and observed journals. Production
generation, frozen corpus and book schema remain unchanged.
"""

from __future__ import annotations

import copy
import hashlib
from collections import defaultdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .checker import reconcile
from .contracts import EpisodeBundle, SCHEMA_VERSION, canonical_json, evidence_registry, validate_public_privacy
from .generator import generate
from .money import convert_minor
from .multi_generator import _policy as _declared_v2_policy

SHOWCASE_VERSION = "1.0"
CASE_ORDER = (
    "clean", "duplicate_only", "fx_only", "masked_both",
    "allocation_fx", "allocation_fx_repaired",
    "allocation_cutoff", "allocation_cutoff_repaired",
)
_LABELS = {
    "clean": "Корректный учёт двух счетов",
    "duplicate_only": "Один счёт признан дважды",
    "fx_only": "Применён курс другой даты",
    "masked_both": "Две ошибки скрылись в общем итоге",
    "allocation_fx": "Распределение платежа и неправильный курс",
    "allocation_fx_repaired": "Курс исправлен; распределение ещё неверно",
    "allocation_cutoff": "Распределение и неправильный отчётный период",
    "allocation_cutoff_repaired": "Период исправлен; распределение ещё неверно",
}


class ShowcaseError(ValueError):
    """A demonstration does not match the independently checked public facts."""


def _id(label: str) -> str:
    return "opaque_" + hashlib.sha256(f"genesis-showcase:{SHOWCASE_VERSION}:{label}".encode()).hexdigest()[:32]


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _update_counts(public: dict) -> None:
    counts = {table: len(public[table]) for table in ("ledger", "invoices", "payments", "fx")}
    public["manifest"].update(ledger_rows=counts["ledger"], document_count=counts["invoices"] + counts["payments"], table_counts=counts)


def _bundle(public: dict, *, scenario: str, provenance: dict | None = None) -> EpisodeBundle:
    public = copy.deepcopy(public)
    _update_counts(public)
    validate_public_privacy(public)
    derived = reconcile(public)
    registry = evidence_registry(public)
    if any(not 0 < len(proof) <= 10 or not set(proof) <= registry for issue in derived for proof in issue["evidence_sets"]):
        raise ShowcaseError("invalid_showcase_witness")
    history = copy.deepcopy(provenance or {})
    history.update(schema_version=SCHEMA_VERSION, showcase_version=SHOWCASE_VERSION,
                   showcase_case=scenario, public_sha256=_digest(public))
    return EpisodeBundle(public=public, truth=derived, provenance=history)


def _masking_world() -> dict:
    issued = "2026-11-05T12:00:00+05:00"
    invoices = []
    for key, currency, amount in (("native", "KZT", 5_000_000), ("foreign", "USD", 100_000)):
        invoices.append({
            "doc_id": _id(f"document:{key}"), "invoice_id": _id(f"invoice:{key}"),
            "entity_id": _id(f"customer:{key}"), "issued_at": issued,
            "due_at": "2026-11-30T12:00:00+05:00", "currency": currency,
            "total_minor": amount,
            "line_items": [{"description": "Документированная поставка", "quantity": 1,
                            "unit_price_minor": amount, "total_minor": amount}],
        })
    ledger = []
    for invoice in invoices:
        key = "native" if invoice["currency"] == "KZT" else "foreign"
        base = 5_000_000 if key == "native" else 50_000_000
        metadata = {
            "timestamp": issued, "currency": "KZT", "entity_id": invoice["entity_id"],
            "invoice_id": invoice["invoice_id"], "payment_id": None,
            "journal_id": _id(f"journal:{key}"), "source_currency": invoice["currency"],
            "source_amount_minor": invoice["total_minor"], "applied_fx_date": "2026-11-05",
            "applied_fx_rate": "1" if key == "native" else "500", "allocated_minor": None,
        }
        ledger.extend([
            {**metadata, "row_id": _id(f"row:{key}:primary"), "account": "AR", "debit_minor": base, "credit_minor": 0},
            {**metadata, "row_id": _id(f"row:{key}:revenue"), "account": "REVENUE", "debit_minor": 0, "credit_minor": base},
        ])
    public = {
        "manifest": {
            "schema_version": SCHEMA_VERSION, "episode_id": _id("episode:masking"),
            "base_currency": "KZT", "currency_exponents": {"KZT": 2, "USD": 2},
            "period_start": "2026-11-01T00:00:00+05:00", "period_end": "2026-12-01T00:00:00+05:00",
            "timezone": "Asia/Almaty", "rounding": "ROUND_HALF_EVEN",
        },
        "policy": _declared_v2_policy(), "ledger": ledger,
        "invoices": invoices, "payments": [],
        "fx": [
            {"record_id": _id("fx:previous"), "date": "2026-11-04", "source_currency": "USD", "target_currency": "KZT", "rate": "450"},
            {"record_id": _id("fx:current"), "date": "2026-11-05", "source_currency": "USD", "target_currency": "KZT", "rate": "500"},
        ],
    }
    _update_counts(public)
    if reconcile(public):
        raise ShowcaseError("masking_source_world_is_not_clean")
    return public


def _mask_variant(clean: dict, *, duplicate: bool, wrong_fx: bool) -> dict:
    public = copy.deepcopy(clean)
    if duplicate:
        native = next(invoice for invoice in public["invoices"] if invoice["currency"] == "KZT")
        originals = [row for row in public["ledger"] if row["invoice_id"] == native["invoice_id"]]
        for original in originals:
            copied = copy.deepcopy(original)
            copied["journal_id"] = _id("journal:additional_recognition")
            copied["row_id"] = _id("additional:" + original["row_id"])
            public["ledger"].append(copied)
    if wrong_fx:
        for row in public["ledger"]:
            if row["source_currency"] == "USD":
                row["applied_fx_date"], row["applied_fx_rate"] = "2026-11-04", "450"
                row["debit_minor"] = 45_000_000 if row["debit_minor"] else 0
                row["credit_minor"] = 45_000_000 if row["credit_minor"] else 0
    _update_counts(public)
    return public


def _date(public: dict, timestamp: str) -> str:
    return datetime.fromisoformat(timestamp).astimezone(ZoneInfo(public["manifest"]["timezone"])).date().isoformat()


def _rate(public: dict, currency: str, timestamp: str) -> tuple[str, str]:
    day = _date(public, timestamp)
    if currency == public["manifest"]["base_currency"]:
        return "1", day
    rows = [row for row in public["fx"] if row["date"] == day and row["source_currency"] == currency and row["target_currency"] == public["manifest"]["base_currency"]]
    if len(rows) != 1:
        raise ShowcaseError("missing_or_ambiguous_showcase_fx")
    return rows[0]["rate"], day


def _convert(public: dict, amount: int, currency: str, rate: str) -> int:
    exponents = public["manifest"]["currency_exponents"]
    return convert_minor(amount, rate, exponents[currency], exponents[public["manifest"]["base_currency"]], rounding=public["policy"]["rounding"])


def repair_payment_fx(public: dict) -> dict:
    """Repair wrong foreign receipt valuations using public facts, without gold.

    Keep source documents, source allocations and posting periods unchanged.
    Rebuild the same atomic journal's observed counterparts at the policy rate.
    """
    repaired = copy.deepcopy(public)
    reconcile(repaired)
    accounts = repaired["policy"]["accounts"]
    invoices = {doc["invoice_id"]: doc for doc in repaired["invoices"]}
    payments = {doc["payment_id"]: doc for doc in repaired["payments"]}
    journals = defaultdict(list)
    for row in repaired["ledger"]:
        journals[row["journal_id"]].append(row)
    replacements = {}
    for journal_id, rows in journals.items():
        first = rows[0]
        payment = payments.get(first["payment_id"])
        if payment is None or payment["amount_minor"] <= 0 or payment["currency"] == repaired["manifest"]["base_currency"]:
            continue
        primary = next(row for row in rows if row["account"] == accounts["cash"])
        correct_rate, correct_date = _rate(repaired, payment["currency"], payment["timestamp"])
        cash = _convert(repaired, payment["amount_minor"], payment["currency"], correct_rate)
        if primary["debit_minor"] - primary["credit_minor"] == cash:
            continue
        refs = payment["invoice_refs"]
        if not isinstance(refs, list) or len(refs) != 1:
            raise ShowcaseError("repair_requires_documented_payment_designation")
        invoice = invoices[refs[0]]
        allocated = primary["allocated_minor"]
        invoice_rate, _ = _rate(repaired, invoice["currency"], invoice["issued_at"])
        carrying = _convert(repaired, allocated, invoice["currency"], invoice_rate)
        suspense = _convert(repaired, payment["amount_minor"] - allocated, payment["currency"], correct_rate)
        gain = cash - carrying - suspense
        metadata = {key: value for key, value in primary.items() if key not in {"row_id", "account", "debit_minor", "credit_minor"}}
        metadata.update(applied_fx_date=correct_date, applied_fx_rate=correct_rate)
        by_account = {row["account"]: row["row_id"] for row in rows}
        new_rows = []
        for account, signed_debit in (
            (accounts["cash"], cash), (accounts["receivable"], -carrying),
            (accounts["unallocated"], -suspense),
            (accounts["fx_gain"] if gain >= 0 else accounts["fx_loss"], -gain),
        ):
            if not signed_debit:
                continue
            new_rows.append({
                **metadata, "row_id": by_account.get(account, _id(journal_id + ":repair:" + account)),
                "account": account, "debit_minor": max(signed_debit, 0), "credit_minor": max(-signed_debit, 0),
                "invoice_id": None if account == accounts["unallocated"] else invoice["invoice_id"],
            })
        replacements[journal_id] = new_rows
    if not replacements:
        raise ShowcaseError("no_foreign_receipt_fx_error_to_repair")
    repaired["ledger"] = [row for row in repaired["ledger"] if row["journal_id"] not in replacements]
    for rows in replacements.values():
        repaired["ledger"].extend(rows)
    _update_counts(repaired)
    reconcile(repaired)
    return repaired


def repair_period_cutoff(public: dict) -> dict:
    """Restore a wrong journal's source period using public document timestamps."""
    repaired = copy.deepcopy(public)
    reconcile(repaired)
    start, end = (datetime.fromisoformat(repaired["manifest"][key]) for key in ("period_start", "period_end"))
    invoices = {doc["invoice_id"]: doc for doc in repaired["invoices"]}
    payments = {doc["payment_id"]: doc for doc in repaired["payments"]}
    changes = 0
    for row in repaired["ledger"]:
        source = payments[row["payment_id"]]["timestamp"] if row["payment_id"] is not None else invoices[row["invoice_id"]]["issued_at"]
        if (start <= datetime.fromisoformat(row["timestamp"]) < end) != (start <= datetime.fromisoformat(source) < end):
            row["timestamp"] = source
            changes += 1
    if not changes:
        raise ShowcaseError("no_cutoff_error_to_repair")
    reconcile(repaired)
    return repaired


def _repair_bundle(original: EpisodeBundle, public: dict, key: str, operation: str) -> EpisodeBundle:
    provenance = copy.deepcopy(original.provenance)
    provenance["intended_issues_scope"] = "historical_pre_counterfactual_generation"
    provenance.setdefault("counterfactual_repairs", []).append({
        "operation": operation, "before_public_sha256": _digest(original.public),
        "after_public_sha256": _digest(public), "inputs": "public_documents_policy_and_observed_journals_only",
    })
    return _bundle(public, scenario=key, provenance=provenance)


def showcase_cases() -> dict[str, EpisodeBundle]:
    """Eight deterministic, openly documented scenarios for actual policy runs."""
    clean = _masking_world()
    cases = {}
    for key, duplicate, fx in (("clean", False, False), ("duplicate_only", True, False), ("fx_only", False, True), ("masked_both", True, True)):
        public = _mask_variant(clean, duplicate=duplicate, wrong_fx=fx)
        cases[key] = _bundle(public, scenario=key, provenance={
            "clean_public_sha256": _digest(clean),
            "history": {"invoices": copy.deepcopy(clean["invoices"]), "payments": []},
            "changes": [name for name, enabled in (("repeat_native_invoice_recognition", duplicate), ("use_previous_day_fx450_instead_of500", fx)) if enabled],
        })
    base_config = {"invoice_count": 6, "ledger_rows": None, "foreign_invoice_count": 3,
                   "refund_count": 0, "duplicate_count": 0, "allocation_count": 1}
    fx = generate(19, {**base_config, "fx_count": 1, "cutoff_count": 0, "interaction_allocation_fx": True})
    cutoff = generate(19, {**base_config, "fx_count": 0, "cutoff_count": 1, "interaction_allocation_cutoff": True})
    cases["allocation_fx"] = fx
    cases["allocation_fx_repaired"] = _repair_bundle(fx, repair_payment_fx(fx.public), "allocation_fx_repaired", "correct_foreign_receipt_valuation")
    cases["allocation_cutoff"] = cutoff
    cases["allocation_cutoff_repaired"] = _repair_bundle(cutoff, repair_period_cutoff(cutoff.public), "allocation_cutoff_repaired", "restore_source_reporting_period")
    return cases


def _source_facts(public: dict) -> str:
    manifest = {key: public["manifest"][key] for key in (
        "schema_version", "base_currency", "currency_exponents", "period_start", "period_end", "timezone", "rounding",
    )}
    return canonical_json({
        "manifest": manifest, "policy": public["policy"],
        **{table: sorted(public[table], key=lambda row: row["record_id"] if table == "fx" else row["doc_id"])
           for table in ("invoices", "payments", "fx")},
    })


def _totals(public: dict) -> tuple[int, int]:
    accounts = public["policy"]["accounts"]
    recognition = sum(row["debit_minor"] - row["credit_minor"] for row in public["ledger"]
                      if row["payment_id"] is None and row["account"] == accounts["receivable"])
    revenue = sum(row["credit_minor"] - row["debit_minor"] for row in public["ledger"] if row["account"] == accounts["revenue"])
    return recognition, revenue


def _calculation(public: dict, issue: dict) -> dict:
    rows = {row["row_id"]: row for row in public["ledger"]}
    invoices = {doc["invoice_id"]: doc for doc in public["invoices"]}
    payments = {doc["payment_id"]: doc for doc in public["payments"]}
    accounts = public["policy"]["accounts"]
    kind = issue["type"]
    if kind == "payment_allocation":
        payment = next(payments[identity] for identity in issue["affected_ids"] if identity in payments)
        journal = [row for row in public["ledger"] if row["payment_id"] == payment["payment_id"]]
        primary = next(row for row in journal if row["account"] == accounts["cash"])
        remainder = payment["amount_minor"] - primary["allocated_minor"]
        observed = sum(row["credit_minor"] - row["debit_minor"] for row in journal if row["account"] == accounts["unallocated"])
        return {
            "formula": "convert(source_amount − allocated_source, observed FX)",
            "source_amount_minor": payment["amount_minor"], "source_currency": payment["currency"],
            "source_exponent": public["manifest"]["currency_exponents"][payment["currency"]], "base_exponent": 2,
            "allocated_source_minor": primary["allocated_minor"], "unallocated_source_minor": remainder,
            "applied_fx_date": primary["applied_fx_date"], "applied_fx_rate": primary["applied_fx_rate"],
            "observed_base_minor": observed, "expected_base_minor": 0,
            "discrepancy_minor": _convert(public, remainder, payment["currency"], primary["applied_fx_rate"]),
        }
    primary = rows[issue["affected_ids"][0]]
    document = invoices[primary["invoice_id"]] if primary["payment_id"] is None else payments[primary["payment_id"]]
    is_invoice = primary["payment_id"] is None
    amount = document["total_minor" if is_invoice else "amount_minor"]
    timestamp = document["issued_at" if is_invoice else "timestamp"]
    observed = primary["debit_minor"] + primary["credit_minor"]
    common = {"source_amount_minor": amount, "source_currency": document["currency"],
              "source_exponent": public["manifest"]["currency_exponents"][document["currency"]], "base_exponent": 2}
    if kind == "duplicate_invoice":
        primaries = [rows[identity] for identity in issue["affected_ids"]]
        total = sum(row["debit_minor"] for row in primaries)
        return {**common, "formula": "Σ recognition − one documented recognition", "recognition_count": len(primaries),
                "observed_base_minor": total, "expected_base_minor": observed, "discrepancy_minor": total - observed}
    if kind == "fx_conversion":
        correct_rate, correct_date = _rate(public, document["currency"], timestamp)
        expected = abs(_convert(public, amount, document["currency"], correct_rate))
        return {**common, "formula": "|observed_base − convert(source, policy FX)|",
                "source_timestamp": timestamp, "policy_timezone": public["manifest"]["timezone"],
                "correct_fx_date": correct_date, "correct_fx_rate": correct_rate,
                "applied_fx_date": primary["applied_fx_date"], "applied_fx_rate": primary["applied_fx_rate"],
                "observed_base_minor": observed, "expected_base_minor": expected, "discrepancy_minor": abs(observed - expected)}
    if kind == "period_cutoff":
        start, end = (datetime.fromisoformat(public["manifest"][key]) for key in ("period_start", "period_end"))
        return {**common, "formula": "period(source time) ≠ period(posted time)", "source_timestamp": timestamp,
                "posted_timestamp": primary["timestamp"], "period_start": public["manifest"]["period_start"],
                "period_end": public["manifest"]["period_end"], "policy_timezone": public["manifest"]["timezone"],
                "source_in_period": start <= datetime.fromisoformat(timestamp) < end,
                "posted_in_period": start <= datetime.fromisoformat(primary["timestamp"]) < end,
                "observed_base_minor": observed, "discrepancy_minor": observed}
    raise ShowcaseError("unsupported_showcase_issue_type")


def showcase_analysis(cases: dict[str, EpisodeBundle]) -> dict:
    """Derive mathematical cards for post-run review; never contains agent scores.

    The orchestrator must publish these cards only after every closed run ends.
    Gross absolute effects below refer solely to one recognition/revenue bridge,
    never to a universal sum of issue amounts or professional audit damages.
    """
    if type(cases) is not dict or set(cases) != set(CASE_ORDER):
        raise ShowcaseError("showcase_requires_eight_named_cases")
    cards = []
    derived = {}
    for key in CASE_ORDER:
        bundle = cases[key]
        validate_public_privacy(bundle.public)
        issues = reconcile(bundle.public)
        if issues != bundle.truth:
            raise ShowcaseError("showcase_gold_does_not_match_public_reconciliation")
        derived[key] = issues
        recognition, revenue = _totals(bundle.public)
        details = []
        for issue in issues:
            calculation = _calculation(bundle.public, issue)
            if calculation["discrepancy_minor"] != issue["expected_amount_minor"]:
                raise ShowcaseError("showcase_calculation_does_not_match_issue_amount")
            details.append({"type": issue["type"], "affected_ids": issue["affected_ids"],
                            "amount_minor": issue["expected_amount_minor"], "currency": issue["currency"],
                            "evidence_sets": issue["evidence_sets"], "calculation": calculation})
        cards.append({"key": key, "label": _LABELS[key], "episode_id": bundle.public["manifest"]["episode_id"],
                      "recognition_total_minor": recognition, "revenue_total_minor": revenue,
                      "issue_count": len(issues), "issues": details})
    totals = {card["key"]: card["recognition_total_minor"] for card in cards}
    duplicate_delta = totals["duplicate_only"] - totals["clean"]
    fx_delta = totals["fx_only"] - totals["clean"]
    masking = {
        "clean_key": "clean", "masked_key": "masked_both",
        "clean_total_minor": totals["clean"], "masked_total_minor": totals["masked_both"],
        "signed_effects": [
            {"type": "duplicate_invoice", "signed_minor": duplicate_delta, "scope": "recognition_and_revenue_projection"},
            {"type": "fx_conversion", "signed_minor": fx_delta, "scope": "recognition_and_revenue_projection"},
        ],
        "net_effect_minor": totals["masked_both"] - totals["clean"],
        "gross_absolute_effect_minor": abs(duplicate_delta) + abs(fx_delta),
        "source_facts_equal": len({_source_facts(cases[key].public) for key in CASE_ORDER[:4]}) == 1,
        "scope": "One recognition/revenue aggregate only; gross absolute counterfactual effects are not total financial damages or a sum of arbitrary issue amounts.",
    }

    def issue_amount(key: str, kind: str) -> int:
        return sum(issue["expected_amount_minor"] for issue in derived[key] if issue["type"] == kind)

    fx_repair = {
        "before_key": "allocation_fx", "after_key": "allocation_fx_repaired",
        "before_allocation_minor": issue_amount("allocation_fx", "payment_allocation"),
        "after_allocation_minor": issue_amount("allocation_fx_repaired", "payment_allocation"),
        "allocation_change_minor": issue_amount("allocation_fx_repaired", "payment_allocation") - issue_amount("allocation_fx", "payment_allocation"),
        "before_fx_error_minor": issue_amount("allocation_fx", "fx_conversion"),
        "after_fx_error_minor": issue_amount("allocation_fx_repaired", "fx_conversion"),
        "source_facts_equal": _source_facts(cases["allocation_fx"].public) == _source_facts(cases["allocation_fx_repaired"].public),
    }
    cutoff_repair = {
        "before_key": "allocation_cutoff", "after_key": "allocation_cutoff_repaired",
        "before_cutoff_minor": issue_amount("allocation_cutoff", "period_cutoff"),
        "after_cutoff_minor": issue_amount("allocation_cutoff_repaired", "period_cutoff"),
        "before_allocation_minor": issue_amount("allocation_cutoff", "payment_allocation"),
        "after_allocation_minor": issue_amount("allocation_cutoff_repaired", "payment_allocation"),
        "source_facts_equal": _source_facts(cases["allocation_cutoff"].public) == _source_facts(cases["allocation_cutoff_repaired"].public),
    }
    expected_types = {
        "clean": [], "duplicate_only": ["duplicate_invoice"], "fx_only": ["fx_conversion"],
        "masked_both": ["duplicate_invoice", "fx_conversion"],
        "allocation_fx": ["fx_conversion", "payment_allocation"],
        "allocation_fx_repaired": ["payment_allocation"],
        "allocation_cutoff": ["payment_allocation", "period_cutoff"],
        "allocation_cutoff_repaired": ["payment_allocation"],
    }
    checks = {
        "all_variants_independently_reconciled": True,
        "exact_issue_families": all(sorted(issue["type"] for issue in derived[key]) == sorted(expected_types[key]) for key in CASE_ORDER),
        "masking_source_facts_unchanged": masking["source_facts_equal"],
        "masking_clean_and_dirty_both_550000_kzt": totals["clean"] == totals["masked_both"] == 55_000_000,
        "opposite_effects_plus_minus_50000_kzt": duplicate_delta == 5_000_000 and fx_delta == -5_000_000,
        "zero_net_100000_kzt_gross_projection_effect": masking["net_effect_minor"] == 0 and masking["gross_absolute_effect_minor"] == 10_000_000,
        "recognition_and_revenue_projection_agree": all(card["recognition_total_minor"] == card["revenue_total_minor"] for card in cards),
        "fx_repair_changes_allocation_amount_without_source_changes": fx_repair["source_facts_equal"] and fx_repair["before_allocation_minor"] > 0 and fx_repair["after_allocation_minor"] > 0 and fx_repair["allocation_change_minor"] != 0 and fx_repair["before_fx_error_minor"] > 0 and fx_repair["after_fx_error_minor"] == 0,
        "cutoff_repair_preserves_allocation_without_source_changes": cutoff_repair["source_facts_equal"] and cutoff_repair["before_cutoff_minor"] > 0 and cutoff_repair["after_cutoff_minor"] == 0 and cutoff_repair["before_allocation_minor"] == cutoff_repair["after_allocation_minor"] > 0,
        "all_proofs_complete_and_within_ten": all(0 < len(proof) <= 10 and set(proof) <= evidence_registry(cases[key].public) for key in CASE_ORDER for issue in derived[key] for proof in issue["evidence_sets"]),
    }
    if not all(type(value) is bool for value in checks.values()):
        raise ShowcaseError("showcase_check_requires_boolean")
    return {"showcase_version": SHOWCASE_VERSION, "scope": "post_run_only", "currency": "KZT",
            "case_order": list(CASE_ORDER), "cards": cards, "masking": masking,
            "dependent_fx": fx_repair, "dependent_cutoff": cutoff_repair, "checks": checks}
