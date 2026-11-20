"""Deterministic causal books for the first native-currency vertical slice."""

from __future__ import annotations

import copy
import hashlib
import random
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .checker import reconcile
from .contracts import SCHEMA_VERSION, EpisodeBundle, canonical_json
from .money import split_minor


class GenerationError(ValueError):
    """The requested configuration cannot produce a valid supported episode."""


_DEFAULTS = {
    "invoice_count": 24,
    "ledger_rows": 200,
    "duplicate_count": 1,
    "allocation_count": 1,
    "allocation_mode": "partial",
    "timezone": "Asia/Almaty",
    "period_start": "2026-11-01T00:00:00+05:00",
    "period_end": "2026-12-01T00:00:00+05:00",
}


def _rng(seed: int, label: str) -> random.Random:
    digest = hashlib.sha256(f"genesis:{SCHEMA_VERSION}:{seed}:{label}".encode()).digest()
    return random.Random(int.from_bytes(digest, "big"))


class _IDs:
    def __init__(self, seed: int) -> None:
        self.rng = _rng(seed, "presentation_ids")
        self.seen: set[str] = {"policy"}

    def new(self, prefix: str) -> str:
        while True:
            candidate = f"{prefix}_{self.rng.getrandbits(128):032x}"
            if candidate not in self.seen:
                self.seen.add(candidate)
                return candidate


def _configuration(config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise GenerationError("config_requires_object")
    if set(config) - set(_DEFAULTS):
        raise GenerationError("unsupported_stage1_config_key")
    result = {**_DEFAULTS, **config}
    for key, minimum, maximum in (
        ("invoice_count", 3, 100),
        ("duplicate_count", 0, 12),
        ("allocation_count", 0, 12),
    ):
        value = result[key]
        if type(value) is not int or not minimum <= value <= maximum:
            raise GenerationError(f"invalid_{key}")
    if result["duplicate_count"] + result["allocation_count"] > 12:
        raise GenerationError("issue_count_exceeds_supported_limit")
    if result["duplicate_count"] > result["invoice_count"]:
        raise GenerationError("not_enough_duplicate_targets")
    target = result["ledger_rows"]
    if target is not None and (type(target) is not int or not 1 <= target <= 2000):
        raise GenerationError("invalid_ledger_rows")
    if result["allocation_mode"] not in ("partial", "missing"):
        raise GenerationError("invalid_allocation_mode")
    try:
        timezone = ZoneInfo(result["timezone"])
        start = datetime.fromisoformat(result["period_start"])
        end = datetime.fromisoformat(result["period_end"])
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise GenerationError("invalid_reporting_period") from exc
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise GenerationError("invalid_reporting_period")
    if end - start < timedelta(seconds=1):
        raise GenerationError("reporting_period_too_short")
    result["period_start"] = start.astimezone(timezone).isoformat()
    result["period_end"] = end.astimezone(timezone).isoformat()
    return result


def _policy() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "accounting_model": "receivables_native_v1",
        "accounts": {"receivable": "AR", "revenue": "REVENUE", "cash": "CASH", "unallocated": "UNALLOCATED"},
        "allocation_rule": "atomic_full_designation",
        "invoice_recognition": "issued_at",
        "payment_recognition": "timestamp",
        "period_bounds": "[start,end)",
        "rounding": "ROUND_HALF_EVEN",
        "amount_currency": "base_currency",
        "amount_tolerance_minor": 0,
        "canonical_rules": {
            "duplicate_invoice": "all_receivable_debit_recognition_row_ids_including_original",
            "payment_allocation": "payment_id_and_invoice_id",
        },
        "amount_rules": {
            "duplicate_invoice": "excess_gross_recognition_in_base_minor_units",
            "payment_allocation": "excess_unallocated_receipt_credit_in_observed_valuation",
        },
        "unknown_payment_designation": "do_not_infer_an_invoice_from_entity_date_or_amount",
        "receipt_lifecycle": "one_atomic_journal_no_later_clearing",
    }


def _semantic_issues(issues: list[dict[str, Any]]) -> list[str]:
    return sorted(canonical_json({
        key: issue[key]
        for key in ("type", "affected_ids", "expected_amount_minor", "currency")
    }) for issue in issues)


def generate(seed: int, config: dict[str, Any] | None = None) -> EpisodeBundle:
    """Build a clean causal world, materialize it, mutate it, then blind-check it.

    ``ledger_rows`` sizes the clean baseline; controlled mutations can add rows.
    Set it to None for a small handwritten-style fixture. The final supported
    episode still cannot exceed 2000 ledger rows or 200 source documents.
    """
    if type(seed) is not int:
        raise GenerationError("seed_requires_integer")
    options = _configuration({} if config is None else config)
    world_rng, mutation_rng = _rng(seed, "world"), _rng(seed, "anomalies")
    order_rng = _rng(seed, "presentation_order")
    ids = _IDs(seed)
    episode_id = ids.new("episode")
    entities = [ids.new("entity") for _ in range(5)]
    start = datetime.fromisoformat(options["period_start"])
    end = datetime.fromisoformat(options["period_end"])
    duration = end - start
    invoice_count = options["invoice_count"]
    repeated_total = world_rng.randint(50_000, 150_000) * 100
    invoices = []
    payments = []
    invoice_entity = {}
    for index in range(invoice_count):
        invoice_id = ids.new("invoice")
        issued = start + duration * (index + 1) / (4 * (invoice_count + 1))
        total = repeated_total if index < 2 else world_rng.randint(20_000, 250_000) * 100
        entity = entities[index % len(entities)]
        invoice_entity[invoice_id] = entity
        invoice = {
            "doc_id": ids.new("doc"), "invoice_id": invoice_id, "entity_id": entity,
            "issued_at": issued.isoformat(), "due_at": (issued + timedelta(days=30)).isoformat(),
            "currency": "KZT", "total_minor": total, "line_items": [],
        }
        invoices.append(invoice)
        # The first invoice is legitimately paid in two instalments; the next
        # has a real outstanding balance; the third is legitimately unpaid.
        if index == 0:
            amounts = split_minor(total, 2)
        elif index == 2 or index % 5 == 4:
            amounts = []
        elif index == 1 or index % 5 == 1:
            amounts = [total // 2]
        else:
            amounts = [total]
        for payment_index, amount in enumerate(amounts):
            timestamp = issued + duration / 3 + duration * payment_index / 10
            payments.append({
                "doc_id": ids.new("doc"), "payment_id": ids.new("payment"),
                "timestamp": timestamp.isoformat(), "currency": "KZT",
                "amount_minor": amount, "invoice_refs": [invoice_id],
            })
    if len(invoices) + len(payments) > 200:
        raise GenerationError("document_budget_exceeded")
    if options["allocation_count"] > len(payments):
        raise GenerationError("not_enough_allocation_targets")

    baseline_rows = 2 * (len(invoices) + len(payments))
    target_rows = options["ledger_rows"]
    target_rows = baseline_rows if target_rows is None else target_rows
    if target_rows < baseline_rows:
        raise GenerationError("ledger_rows_too_small_for_source_documents")
    line_counts = [1] * invoice_count
    for _ in range(target_rows - baseline_rows):
        candidates = [index for index, count in enumerate(line_counts) if count < 100]
        if not candidates:
            raise GenerationError("ledger_rows_exceed_line_item_capacity")
        line_counts[world_rng.choice(candidates)] += 1
    descriptions = ("Оборудование", "Техническая поддержка", "Обслуживание", "Консультация")
    for invoice, count in zip(invoices, line_counts, strict=True):
        amounts = split_minor(invoice["total_minor"], count)
        world_rng.shuffle(amounts)
        invoice["line_items"] = [{
            "description": descriptions[index % len(descriptions)], "quantity": 1,
            "unit_price_minor": amount, "total_minor": amount,
        } for index, amount in enumerate(amounts)]

    ledger: list[dict[str, Any]] = []

    def row(
        timestamp: str, account: str, amount: int, side: str,
        invoice_id: str | None, payment_id: str | None, entity: str,
    ) -> dict[str, Any]:
        result = {
            "row_id": ids.new("row"), "timestamp": timestamp, "account": account,
            "debit_minor": amount if side == "debit" else 0,
            "credit_minor": amount if side == "credit" else 0,
            "currency": "KZT", "entity_id": entity,
            "invoice_id": invoice_id, "payment_id": payment_id,
        }
        ledger.append(result)
        return result

    for invoice in invoices:
        invoice_id = invoice["invoice_id"]
        row(invoice["issued_at"], "AR", invoice["total_minor"], "debit", invoice_id, None, invoice["entity_id"])
        for line in invoice["line_items"]:
            row(invoice["issued_at"], "REVENUE", line["total_minor"], "credit", invoice_id, None, invoice["entity_id"])
    for payment in payments:
        invoice_id = payment["invoice_refs"][0]
        entity = invoice_entity[invoice_id]
        row(payment["timestamp"], "CASH", payment["amount_minor"], "debit", invoice_id, payment["payment_id"], entity)
        row(payment["timestamp"], "AR", payment["amount_minor"], "credit", invoice_id, payment["payment_id"], entity)

    public = {
        "manifest": {
            "schema_version": SCHEMA_VERSION, "episode_id": episode_id,
            "base_currency": "KZT", "period_start": options["period_start"],
            "period_end": options["period_end"], "timezone": options["timezone"],
            "currency_exponents": {"KZT": 2}, "rounding": "ROUND_HALF_EVEN",
        },
        "policy": _policy(), "ledger": ledger, "invoices": invoices, "payments": payments, "fx": [],
    }

    def update_counts() -> None:
        counts = {name: len(public[name]) for name in ("ledger", "invoices", "payments", "fx")}
        public["manifest"].update(ledger_rows=counts["ledger"], document_count=counts["invoices"] + counts["payments"], table_counts=counts)

    update_counts()
    if reconcile(public):
        raise GenerationError("clean_world_failed_independent_validation")
    clean_digest = hashlib.sha256(canonical_json(public).encode()).hexdigest()
    intended = []
    changes = []
    duplicate_targets = mutation_rng.sample(invoices, options["duplicate_count"])
    for invoice in duplicate_targets:
        invoice_id = invoice["invoice_id"]
        source_rows = [entry for entry in ledger if entry["invoice_id"] == invoice_id and entry["payment_id"] is None]
        added = []
        for original in source_rows:
            copied = copy.deepcopy(original)
            copied["row_id"] = ids.new("row")
            ledger.append(copied)
            added.append(copied["row_id"])
        primary_ids = sorted(entry["row_id"] for entry in ledger if entry["invoice_id"] == invoice_id and entry["payment_id"] is None and entry["account"] == "AR")
        intended.append({"type": "duplicate_invoice", "affected_ids": primary_ids, "expected_amount_minor": invoice["total_minor"], "currency": "KZT"})
        changes.append({"type": "duplicate_invoice", "invoice_id": invoice_id, "added_row_ids": added, "causal_change": "recognise_one_existing_invoice_twice"})
    allocation_targets = mutation_rng.sample(payments, options["allocation_count"])
    for payment in allocation_targets:
        payment_id = payment["payment_id"]
        invoice_id = payment["invoice_refs"][0]
        allocated_row = next(entry for entry in ledger if entry["payment_id"] == payment_id and entry["account"] == "AR")
        omitted = payment["amount_minor"] if options["allocation_mode"] == "missing" else mutation_rng.randint(1, payment["amount_minor"] - 1)
        original_amount = allocated_row["credit_minor"]
        allocated_row["credit_minor"] -= omitted
        if allocated_row["credit_minor"] == 0:
            ledger.remove(allocated_row)
        suspense = row(payment["timestamp"], "UNALLOCATED", omitted, "credit", None, payment_id, invoice_entity[invoice_id])
        intended.append({"type": "payment_allocation", "affected_ids": sorted([payment_id, invoice_id]), "expected_amount_minor": omitted, "currency": "KZT"})
        changes.append({"type": "payment_allocation", "payment_id": payment_id, "invoice_id": invoice_id, "before_allocated_minor": original_amount, "after_allocated_minor": original_amount - omitted, "unallocated_row_id": suspense["row_id"], "causal_change": "move_documented_allocation_to_unallocated_receipts"})
    if len(ledger) > 2000:
        raise GenerationError("mutated_ledger_budget_exceeded")
    update_counts()
    for table in ("ledger", "invoices", "payments"):
        order_rng.shuffle(public[table])
    derived = reconcile(public)
    if _semantic_issues(derived) != _semantic_issues(intended):
        raise GenerationError("independent_truth_does_not_match_intended_changes")
    provenance = {
        "schema_version": SCHEMA_VERSION, "seed": seed, "config": options,
        "clean_public_sha256": clean_digest, "changes": changes,
        "intended_issues": intended,
        "public_sha256": hashlib.sha256(canonical_json(public).encode()).hexdigest(),
    }
    return EpisodeBundle(public=public, truth=derived, provenance=provenance)
