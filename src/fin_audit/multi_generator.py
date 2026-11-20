"""Causal base-currency books with foreign receipts, cutoff and clean refunds.

Only observed accounting metadata is public. Mutation plans, intended answers,
source history and seeds remain private. The independent checker owns derivation
of all four error families and never imports this module.
"""

from __future__ import annotations

import copy
import hashlib
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from .checker import reconcile
from .contracts import SCHEMA_VERSION, EpisodeBundle, canonical_json
from .money import convert_minor, split_minor
from .native_generator import GenerationError, _IDs, _configuration, _policy as _native_policy, _rng, _semantic_issues

_NEW_KEYS = {
    "fx_count", "cutoff_count", "foreign_invoice_count", "foreign_currency",
    "foreign_exponent", "refund_count", "interaction_allocation_fx",
    "interaction_allocation_cutoff", "fx_mode", "fx_target",
}


def _policy() -> dict[str, Any]:
    policy = _native_policy()
    policy["accounting_model"] = "receivables_multi_v2"
    policy["accounts"].update(fx_gain="FX_GAIN", fx_loss="FX_LOSS")
    policy["canonical_rules"].update(fx_conversion="primary_ledger_row_id", period_cutoff="primary_ledger_row_id")
    policy["amount_rules"].update(
        fx_conversion="absolute_primary_valuation_difference_in_base_minor_units",
        period_cutoff="absolute_primary_amount_in_base_minor_units",
    )
    policy.update(
        fx_date_rule="source_document_date_in_company_timezone",
        fx_rate_convention="target_major_per_source_major",
        carrying_rule="correct_invoice_source_date_rate",
        fx_balance_rule="cash_minus_carrying_minus_unallocated",
        refund_rule="native_credit_note_full_designation",
        source_amount_rule="whole_document_amount_per_journal",
    )
    return policy


def _options(config: dict[str, Any]) -> dict[str, Any]:
    native = _configuration({key: value for key, value in config.items() if key not in _NEW_KEYS})
    result = {
        **native,
        "fx_count": 0, "cutoff_count": 0,
        "foreign_invoice_count": min(6, native["invoice_count"] - 3),
        "foreign_currency": "USD", "foreign_exponent": 2, "refund_count": 0,
        "interaction_allocation_fx": False, "interaction_allocation_cutoff": False,
        "fx_mode": "date", "fx_target": "payment",
        **{key: value for key, value in config.items() if key in _NEW_KEYS},
    }
    for key, maximum in (("fx_count", 12), ("cutoff_count", 12), ("refund_count", 10)):
        if type(result[key]) is not int or not 0 <= result[key] <= maximum:
            raise GenerationError(f"invalid_{key}")
    if type(result["foreign_invoice_count"]) is not int or not 0 <= result["foreign_invoice_count"] <= native["invoice_count"] - 3:
        raise GenerationError("invalid_foreign_invoice_count")
    if type(result["foreign_exponent"]) is not int or not 0 <= result["foreign_exponent"] <= 3:
        raise GenerationError("invalid_foreign_exponent")
    if not isinstance(result["foreign_currency"], str) or not re.fullmatch("[A-Z]{3}", result["foreign_currency"]) or result["foreign_currency"] == "KZT":
        raise GenerationError("invalid_foreign_currency")
    for key in ("interaction_allocation_fx", "interaction_allocation_cutoff"):
        if type(result[key]) is not bool:
            raise GenerationError(f"invalid_{key}")
    if result["fx_mode"] not in ("date", "rate") or result["fx_target"] not in ("invoice", "payment"):
        raise GenerationError("invalid_fx_mutation_mode_or_target")
    required_allocations = int(result["interaction_allocation_fx"]) + int(result["interaction_allocation_cutoff"])
    if result["allocation_count"] < required_allocations:
        raise GenerationError("interaction_requires_allocation_count")
    if result["interaction_allocation_fx"] and (result["fx_count"] < 1 or result["fx_target"] != "payment"):
        raise GenerationError("interaction_requires_payment_fx")
    if result["interaction_allocation_cutoff"] and result["cutoff_count"] < 1:
        raise GenerationError("interaction_requires_cutoff_count")
    if sum(result[key] for key in ("duplicate_count", "allocation_count", "fx_count", "cutoff_count")) > 12:
        raise GenerationError("issue_count_exceeds_supported_limit")
    if result["duplicate_count"] > native["invoice_count"] - result["foreign_invoice_count"]:
        raise GenerationError("not_enough_native_duplicate_targets")
    return result


def _row_id(journal_id: str, role: str) -> str:
    return "row_" + hashlib.sha256(f"{journal_id}:{role}".encode()).hexdigest()[:32]


def generate_multi(seed: int, config: dict[str, Any]) -> EpisodeBundle:
    if type(seed) is not int:
        raise GenerationError("seed_requires_integer")
    options = _options(config)
    ids = _IDs(seed)
    world_rng, mutation_rng, order_rng = (_rng(seed, name) for name in ("world_multi", "anomalies_multi", "presentation_multi"))
    episode_id = ids.new("episode")
    entities = [ids.new("entity") for _ in range(5)]
    start, end = (datetime.fromisoformat(options[key]) for key in ("period_start", "period_end"))
    duration = end - start
    company_timezone = ZoneInfo(options["timezone"])
    foreign, exponent = options["foreign_currency"], options["foreign_exponent"]
    currencies = {"KZT": 2}
    if options["foreign_invoice_count"]:
        currencies[foreign] = exponent
    invoices: list[dict[str, Any]] = []
    payments: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    invoice_events = {}
    refund_sources = []
    boundary_receipt = None
    interactive_receipt = None

    def source_date(timestamp: str) -> str:
        return datetime.fromisoformat(timestamp).astimezone(company_timezone).date().isoformat()

    def quoted_rate(date: str) -> str:
        days = (datetime.fromisoformat(date).date() - start.astimezone(company_timezone).date()).days
        value = Decimal(500) + Decimal(days) * Decimal("0.75")
        if value <= 0:
            raise GenerationError("synthetic_fx_rate_not_positive")
        return str(value)

    def new_event(kind: str, document: dict[str, Any], invoice: dict[str, Any]) -> dict[str, Any]:
        timestamp = document["issued_at"] if kind == "invoice" else document["timestamp"]
        date = source_date(timestamp)
        event = {
            "kind": kind, "document": document, "invoice": invoice,
            "journal_id": ids.new("journal"), "posted_at": timestamp,
            "applied_fx_date": date,
            "applied_fx_rate": "1" if document["currency"] == "KZT" else quoted_rate(date),
            "allocated_minor": None if kind == "invoice" else document["amount_minor"],
        }
        events.append(event)
        return event

    repeated_total = world_rng.randint(50_000, 150_000) * 100
    native_index = 0
    for index in range(options["invoice_count"]):
        is_foreign = index < options["foreign_invoice_count"]
        currency = foreign if is_foreign else "KZT"
        if is_foreign:
            total = (1000 if index in (1, 2) else world_rng.randint(500, 8000)) * 10**exponent
            if exponent and index not in (1, 2):
                total += world_rng.randint(1, 10**exponent - 1)
        else:
            total = repeated_total if native_index < 2 else world_rng.randint(20_000, 250_000) * 100
        issued = start + duration * (index + 1) / (4 * (options["invoice_count"] + 1))
        if is_foreign and index == 1:
            issued = start - min(timedelta(hours=12), duration / 10)
        invoice = {
            "doc_id": ids.new("doc"), "invoice_id": ids.new("invoice"),
            "entity_id": entities[index % len(entities)],
            "issued_at": issued.isoformat(), "due_at": (issued + timedelta(days=30)).isoformat(),
            "currency": currency, "total_minor": total,
            "line_items": [{"description": "Поставка", "quantity": 1, "unit_price_minor": total, "total_minor": total}],
        }
        invoices.append(invoice)
        invoice_event = new_event("invoice", invoice, invoice)
        invoice_events[invoice["invoice_id"]] = invoice_event
        if is_foreign:
            amounts = [] if index % 4 == 0 else ([600 * 10**exponent] if index in (1, 2) else [total])
        else:
            if native_index == 0:
                amounts = split_minor(total, 2)
                refund_sources.append(invoice)
            elif native_index == 2 or native_index % 5 == 4:
                amounts = []
            elif native_index == 1 or native_index % 5 == 1:
                amounts = [total // 2]
            else:
                amounts = [total]
                refund_sources.append(invoice)
            native_index += 1
        for payment_index, amount in enumerate(amounts):
            timestamp = issued + duration / 3 + duration * payment_index / 10
            if is_foreign and index == 1:
                timestamp = (start + min(timedelta(minutes=30), duration / 20)).astimezone(timezone.utc)
            if is_foreign and index == 2:
                timestamp = (end - min(timedelta(minutes=30), duration / 20)).astimezone(timezone.utc)
            payment = {
                "doc_id": ids.new("doc"), "payment_id": ids.new("payment"),
                "timestamp": timestamp.isoformat(), "currency": currency,
                "amount_minor": amount, "invoice_refs": [invoice["invoice_id"]],
            }
            payments.append(payment)
            receipt = new_event("payment", payment, invoice)
            if is_foreign and index == 1:
                interactive_receipt = receipt
            if is_foreign and index == 2:
                boundary_receipt = receipt

    for index in range(options["refund_count"]):
        original = refund_sources[index % len(refund_sources)]
        amount = max(1, original["total_minor"] // (options["refund_count"] + 2))
        timestamp = start + duration * (75 + index) / 100
        credit_note = {
            "doc_id": ids.new("doc"), "invoice_id": ids.new("invoice"),
            "entity_id": original["entity_id"], "issued_at": timestamp.isoformat(),
            "due_at": (timestamp + timedelta(days=30)).isoformat(), "currency": "KZT",
            "total_minor": -amount, "original_invoice_id": original["invoice_id"],
            "line_items": [{"description": "Возврат документированной поставки", "quantity": 1, "unit_price_minor": -amount, "total_minor": -amount}],
        }
        invoices.append(credit_note)
        invoice_events[credit_note["invoice_id"]] = new_event("invoice", credit_note, credit_note)
        refund = {
            "doc_id": ids.new("doc"), "payment_id": ids.new("payment"),
            "timestamp": (timestamp + duration / 1000).isoformat(), "currency": "KZT",
            "amount_minor": -amount, "invoice_refs": [credit_note["invoice_id"]],
        }
        payments.append(refund)
        new_event("payment", refund, credit_note)
    if len(invoices) + len(payments) > 200:
        raise GenerationError("document_budget_exceeded")

    fx_dates = set()
    for event in events:
        if event["document"]["currency"] != "KZT":
            date = datetime.fromisoformat(event["applied_fx_date"]).date()
            fx_dates.update((date.isoformat(), (date - timedelta(days=1)).isoformat()))
    if len(fx_dates) > 200:
        raise GenerationError("fx_record_budget_exceeded")
    fx = [{
        "record_id": ids.new("fx"), "date": date,
        "source_currency": foreign, "target_currency": "KZT", "rate": quoted_rate(date),
    } for date in sorted(fx_dates)]

    def converted(amount: int, currency: str, rate: str) -> int:
        return convert_minor(amount, rate, currencies[currency], 2)

    def materialize(event_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ledger = []
        for event in event_list:
            document, invoice = event["document"], event["invoice"]
            is_invoice = event["kind"] == "invoice"
            source_amount = document["total_minor"] if is_invoice else document["amount_minor"]
            currency, rate = document["currency"], event["applied_fx_rate"]
            base = converted(source_amount, currency, rate)
            common = {
                "timestamp": event["posted_at"], "currency": "KZT",
                "entity_id": invoice["entity_id"], "invoice_id": invoice["invoice_id"],
                "payment_id": None if is_invoice else document["payment_id"],
                "journal_id": event["journal_id"], "source_currency": currency,
                "source_amount_minor": source_amount,
                "applied_fx_date": event["applied_fx_date"], "applied_fx_rate": rate,
                "allocated_minor": event["allocated_minor"],
            }

            def add(role: str, account: str, signed_debit: int, *, no_invoice: bool = False) -> None:
                if signed_debit == 0:
                    return
                entry = {
                    **common, "row_id": _row_id(event["journal_id"], role), "account": account,
                    "debit_minor": max(signed_debit, 0), "credit_minor": max(-signed_debit, 0),
                }
                if no_invoice:
                    entry["invoice_id"] = None
                ledger.append(entry)

            if is_invoice:
                add("primary", "AR", base)
                for index, line in enumerate(invoice["line_items"]):
                    add(f"revenue_{index}", "REVENUE", -converted(line["total_minor"], currency, rate))
            elif source_amount < 0:
                add("primary", "CASH", base)
                add("allocation", "AR", -base)
            else:
                allocated = event["allocated_minor"]
                carrying_rate = "1" if invoice["currency"] == "KZT" else quoted_rate(source_date(invoice["issued_at"]))
                carrying = converted(allocated, currency, carrying_rate)
                suspense = converted(source_amount - allocated, currency, rate)
                gain = base - carrying - suspense
                add("primary", "CASH", base)
                add("allocation", "AR", -carrying)
                add("unallocated", "UNALLOCATED", -suspense, no_invoice=True)
                add("fx", "FX_GAIN" if gain > 0 else "FX_LOSS", -gain)
        return ledger

    initial_rows = len(materialize(events))
    target = options["ledger_rows"]
    target = initial_rows if target is None else target
    if target < initial_rows:
        raise GenerationError("ledger_rows_too_small_for_source_documents")
    native_invoices = [doc for doc in invoices if doc["currency"] == "KZT" and doc["total_minor"] > 0]
    line_counts = {doc["invoice_id"]: 1 for doc in native_invoices}
    for _ in range(target - initial_rows):
        available = [doc for doc in native_invoices if line_counts[doc["invoice_id"]] < 100]
        if not available:
            raise GenerationError("ledger_rows_exceed_line_item_capacity")
        chosen = world_rng.choice(available)
        line_counts[chosen["invoice_id"]] += 1
    for invoice in native_invoices:
        amounts = split_minor(invoice["total_minor"], line_counts[invoice["invoice_id"]])
        invoice["line_items"] = [{"description": "Обслуживание", "quantity": 1, "unit_price_minor": amount, "total_minor": amount} for amount in amounts]
    public = {
        "manifest": {
            "schema_version": SCHEMA_VERSION, "episode_id": episode_id,
            "base_currency": "KZT", "period_start": options["period_start"], "period_end": options["period_end"],
            "timezone": options["timezone"], "currency_exponents": currencies, "rounding": "ROUND_HALF_EVEN",
        },
        "policy": _policy(), "ledger": materialize(events), "invoices": invoices, "payments": payments, "fx": fx,
    }

    def counts() -> None:
        table_counts = {name: len(public[name]) for name in ("ledger", "invoices", "payments", "fx")}
        public["manifest"].update(ledger_rows=table_counts["ledger"], document_count=table_counts["invoices"] + table_counts["payments"], table_counts=table_counts)

    counts()
    if reconcile(public):
        raise GenerationError("clean_world_failed_independent_validation")
    clean_digest = hashlib.sha256(canonical_json(public).encode()).hexdigest()
    history = {"invoices": copy.deepcopy(invoices), "payments": copy.deepcopy(payments)}
    positive_receipts = [event for event in events if event["kind"] == "payment" and event["document"]["amount_minor"] > 0]
    foreign_receipts = [event for event in positive_receipts if event["document"]["currency"] != "KZT"]
    unpaid_foreign = [event for event in events if event["kind"] == "invoice" and event["document"]["currency"] != "KZT" and not any(payment["invoice_refs"] == [event["document"]["invoice_id"]] for payment in payments)]
    fx_pool = foreign_receipts if options["fx_target"] == "payment" else unpaid_foreign
    if options["interaction_allocation_cutoff"]:
        if boundary_receipt is None:
            raise GenerationError("not_enough_boundary_interaction_receipts")
        # A requested boundary interaction reserves its event before random FX
        # selection; an otherwise feasible configuration must not randomly fail
        # because another mutation happened to consume the mandatory target.
        fx_pool = [event for event in fx_pool if event is not boundary_receipt]
    if options["fx_count"] > len(fx_pool):
        raise GenerationError("not_enough_foreign_fx_targets")
    forced_fx = [interactive_receipt] if options["interaction_allocation_fx"] else []
    if forced_fx and forced_fx[0] is None:
        raise GenerationError("not_enough_foreign_interaction_receipts")
    fx_targets = forced_fx + mutation_rng.sample([event for event in fx_pool if event not in forced_fx], options["fx_count"] - len(forced_fx))
    cutoff_pool = [event for event in positive_receipts if event not in fx_targets]
    forced_cutoff = [boundary_receipt] if options["interaction_allocation_cutoff"] else []
    if forced_cutoff and (forced_cutoff[0] is None or forced_cutoff[0] not in cutoff_pool):
        raise GenerationError("not_enough_boundary_interaction_receipts")
    if options["cutoff_count"] > len(cutoff_pool):
        raise GenerationError("not_enough_cutoff_targets")
    cutoff_targets = forced_cutoff + mutation_rng.sample([event for event in cutoff_pool if event not in forced_cutoff], options["cutoff_count"] - len(forced_cutoff))
    forced_allocations = forced_fx + forced_cutoff
    allocation_pool = [event for event in positive_receipts if event not in fx_targets and event not in cutoff_targets and event not in forced_allocations]
    if options["allocation_count"] - len(forced_allocations) > len(allocation_pool):
        raise GenerationError("not_enough_disjoint_allocation_targets")
    allocation_targets = forced_allocations + mutation_rng.sample(allocation_pool, options["allocation_count"] - len(forced_allocations))
    duplicate_targets = mutation_rng.sample(native_invoices, options["duplicate_count"])
    changed_events = []
    changes = []
    for invoice in duplicate_targets:
        original = invoice_events[invoice["invoice_id"]]
        duplicated = dict(original, journal_id=ids.new("journal"))
        events.append(duplicated)
        changed_events.append(("duplicate_invoice", duplicated))
        changes.append({"type": "duplicate_invoice", "invoice_id": invoice["invoice_id"], "journal_id": duplicated["journal_id"], "causal_change": "recognise_existing_invoice_twice"})
    for event in allocation_targets:
        amount = event["document"]["amount_minor"]
        if options["allocation_mode"] == "missing":
            omitted = amount
        elif event in forced_allocations:
            omitted = 100 * 10**exponent
        else:
            omitted = max(1, amount // 3)
        event["allocated_minor"] = amount - omitted
        changed_events.append(("payment_allocation", event))
        changes.append({"type": "payment_allocation", "payment_id": event["document"]["payment_id"], "source_omitted_minor": omitted, "causal_change": "leave_documented_designation_unallocated"})
    for event in fx_targets:
        timestamp = event["document"]["issued_at"] if event["kind"] == "invoice" else event["document"]["timestamp"]
        correct_date = source_date(timestamp)
        if options["fx_mode"] == "date":
            actual_date = (datetime.fromisoformat(correct_date).date() - timedelta(days=1)).isoformat()
            event["applied_fx_date"], event["applied_fx_rate"] = actual_date, quoted_rate(actual_date)
        else:
            event["applied_fx_rate"] = str(Decimal(quoted_rate(correct_date)) + Decimal("7.25"))
        changed_events.append(("fx_conversion", event))
        changes.append({"type": "fx_conversion", "journal_id": event["journal_id"], "mode": options["fx_mode"], "causal_change": "use_incorrect_date_or_numeric_rate"})
    for index, event in enumerate(cutoff_targets):
        event["posted_at"] = (end + timedelta(days=1, minutes=index)).isoformat()
        changed_events.append(("period_cutoff", event))
        changes.append({"type": "period_cutoff", "journal_id": event["journal_id"], "causal_change": "post_atomic_journal_in_next_reporting_period"})
    public["ledger"] = materialize(events)
    if len(public["ledger"]) > 2000:
        raise GenerationError("mutated_ledger_budget_exceeded")
    counts()
    intended = []
    for issue_type, event in changed_events:
        document, journal_id = event["document"], event["journal_id"]
        primary_id = _row_id(journal_id, "primary")
        rows = [row for row in public["ledger"] if row["journal_id"] == journal_id]
        primary = next(row for row in rows if row["row_id"] == primary_id)
        primary_amount = primary["debit_minor"] + primary["credit_minor"]
        if issue_type == "duplicate_invoice":
            affected = sorted(row["row_id"] for row in public["ledger"] if row["invoice_id"] == document["invoice_id"] and row["payment_id"] is None and row["account"] == "AR" and row["debit_minor"] > 0)
            amount = primary_amount
        elif issue_type == "payment_allocation":
            affected = sorted([document["payment_id"], event["invoice"]["invoice_id"]])
            amount = sum(row["credit_minor"] for row in rows if row["account"] == "UNALLOCATED")
        elif issue_type == "fx_conversion":
            affected = [primary_id]
            source_amount = document["total_minor"] if event["kind"] == "invoice" else document["amount_minor"]
            timestamp = document["issued_at"] if event["kind"] == "invoice" else document["timestamp"]
            amount = abs(primary_amount - converted(source_amount, document["currency"], quoted_rate(source_date(timestamp))))
        else:
            affected, amount = [primary_id], primary_amount
        if amount <= 0:
            raise GenerationError("zero_effect_mutation_not_allowed")
        intended.append({"type": issue_type, "affected_ids": affected, "expected_amount_minor": amount, "currency": "KZT"})
    for table in ("ledger", "invoices", "payments", "fx"):
        order_rng.shuffle(public[table])
    derived = reconcile(public)
    if _semantic_issues(derived) != _semantic_issues(intended):
        raise GenerationError("independent_truth_does_not_match_intended_changes")
    return EpisodeBundle(public=public, truth=derived, provenance={
        "schema_version": SCHEMA_VERSION, "seed": seed, "config": options,
        "history": history, "clean_public_sha256": clean_digest,
        "changes": changes, "intended_issues": intended,
        "public_sha256": hashlib.sha256(canonical_json(public).encode()).hexdigest(),
    })
