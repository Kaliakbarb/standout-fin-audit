"""Truth-blind reconciliation for the published native-currency stage-one model.

Only public records and public accounting rules enter this module. Inconsistent
or unsupported books are rejected rather than silently called clean. Generator
mutation code, provenance, seeds and proposed truth are deliberately absent.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import SUPPORTED_SCHEMA_VERSIONS, canonical_json
from .checker_errors import DataValidationError
from .money import minor_integer


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise DataValidationError(reason)


def _identifier(value: Any) -> str:
    _require(isinstance(value, str) and 0 < len(value) <= 200, "invalid_record_id")
    return value


def _amount(value: Any, *, positive: bool = False) -> int:
    try:
        result = minor_integer(value, nonnegative=True)
    except ValueError as exc:
        raise DataValidationError(str(exc)) from exc
    _require(not positive or result > 0, "source_amount_must_be_positive")
    return result


def _time(value: Any) -> datetime:
    _require(isinstance(value, str), "timestamp_requires_string")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DataValidationError("invalid_iso_timestamp") from exc
    _require(result.tzinfo is not None and result.utcoffset() is not None, "naive_timestamp")
    return result


def _issue(
    episode_id: str,
    issue_type: str,
    affected_ids: list[str],
    amount_minor: int,
    currency: str,
    evidence_ids: list[str],
) -> dict[str, Any]:
    affected = sorted(set(affected_ids))
    evidence = sorted(set(evidence_ids))
    _require(0 < len(evidence) <= 10, "evidence_budget_exceeded")
    _require(amount_minor > 0, "zero_effect_is_not_an_issue")
    identity = canonical_json([episode_id, issue_type, affected])
    return {
        "issue_id": "issue_" + hashlib.sha256(identity.encode()).hexdigest()[:24],
        "type": issue_type,
        "affected_ids": affected,
        "expected_amount_minor": amount_minor,
        "currency": currency,
        "evidence_sets": [evidence],
    }


def reconcile(
    public: dict[str, Any], policy: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Derive all supported issues from public records, without an expected answer.

    Stage one supports positive KZT receivables, native-currency receipts,
    repeated invoice recognition and missing/partial atomic allocations. It does
    not pretend to validate FX, cutoff or refunds before those stages exist.
    """
    try:
        return _reconcile(public, policy)
    except DataValidationError:
        raise
    except (KeyError, TypeError, IndexError, AttributeError) as exc:
        raise DataValidationError("malformed_public_book") from exc


def _reconcile(
    public: dict[str, Any], policy: dict[str, Any] | None
) -> list[dict[str, Any]]:
    _require(isinstance(public, dict), "public_book_requires_object")
    manifest = public["manifest"]
    actual_policy = public["policy"]
    _require(isinstance(manifest, dict) and isinstance(actual_policy, dict), "invalid_manifest_policy")
    if policy is not None:
        _require(policy == actual_policy, "policy_does_not_match_public_book")
    policy = actual_policy
    _require(manifest["schema_version"] in SUPPORTED_SCHEMA_VERSIONS, "unsupported_schema_version")
    _require(policy.get("schema_version") == manifest["schema_version"], "policy_schema_version_mismatch")
    _require(policy.get("accounting_model") == "receivables_native_v1", "unsupported_accounting_model")
    _require(policy.get("allocation_rule") == "atomic_full_designation", "unsupported_allocation_policy")
    _require(policy.get("period_bounds") == "[start,end)", "unsupported_period_bounds")
    _require(policy.get("invoice_recognition") == "issued_at", "unsupported_invoice_recognition")
    _require(policy.get("payment_recognition") == "timestamp", "unsupported_payment_recognition")
    _require(policy.get("amount_currency") == "base_currency", "unsupported_amount_currency_rule")
    _require(type(policy.get("amount_tolerance_minor")) is int and policy["amount_tolerance_minor"] == 0, "unsupported_stage1_amount_tolerance")
    _require(policy.get("receipt_lifecycle") == "one_atomic_journal_no_later_clearing", "unsupported_receipt_lifecycle")
    _require(policy.get("unknown_payment_designation") == "do_not_infer_an_invoice_from_entity_date_or_amount", "unsupported_unknown_designation_policy")
    _require(policy.get("canonical_rules") == {
        "duplicate_invoice": "all_receivable_debit_recognition_row_ids_including_original",
        "payment_allocation": "payment_id_and_invoice_id",
    }, "unsupported_canonical_rules")
    _require(policy.get("amount_rules") == {
        "duplicate_invoice": "excess_gross_recognition_in_base_minor_units",
        "payment_allocation": "excess_unallocated_receipt_credit_in_observed_valuation",
    }, "unsupported_amount_rules")
    currency = manifest["base_currency"]
    exponents = manifest["currency_exponents"]
    _require(
        currency == "KZT" and type(exponents) is dict and set(exponents) == {"KZT"}
        and type(exponents["KZT"]) is int and exponents["KZT"] == 2,
        "unsupported_stage1_currency",
    )
    episode_id = _identifier(manifest["episode_id"])
    start, end = _time(manifest["period_start"]), _time(manifest["period_end"])
    _require(start < end, "empty_reporting_period")
    try:
        ZoneInfo(manifest["timezone"])
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise DataValidationError("invalid_company_timezone") from exc

    accounts = policy["accounts"]
    account_keys = {"receivable", "revenue", "cash", "unallocated"}
    _require(isinstance(accounts, dict) and set(accounts) == account_keys, "invalid_account_registry")
    _require(all(isinstance(value, str) and value for value in accounts.values()), "invalid_account_name")
    _require(len(set(accounts.values())) == 4, "ambiguous_account_registry")
    ar, revenue = accounts["receivable"], accounts["revenue"]
    cash, unallocated = accounts["cash"], accounts["unallocated"]

    tables = {}
    for name in ("ledger", "invoices", "payments", "fx"):
        _require(isinstance(public[name], list), "table_requires_array")
        tables[name] = public[name]
    _require(not tables["fx"], "fx_not_supported_in_stage1")
    _require(type(manifest["ledger_rows"]) is int and manifest["ledger_rows"] == len(tables["ledger"]), "ledger_count_mismatch")
    _require(type(manifest["document_count"]) is int and manifest["document_count"] == len(tables["invoices"]) + len(tables["payments"]), "document_count_mismatch")
    _require(manifest["table_counts"] == {name: len(rows) for name, rows in tables.items()}, "table_counts_mismatch")
    _require(all(type(count) is int and count >= 0 for count in manifest["table_counts"].values()), "invalid_table_count_type")
    _require(manifest.get("rounding") == policy.get("rounding") == "ROUND_HALF_EVEN", "unsupported_rounding_policy")

    ids = {"policy", episode_id}

    def register(value: Any) -> str:
        identity = _identifier(value)
        _require(identity not in ids, "duplicate_public_identifier")
        ids.add(identity)
        return identity

    invoices = {}
    invoice_lines = {}
    for invoice in tables["invoices"]:
        _require(isinstance(invoice, dict), "invalid_invoice_record")
        register(invoice["doc_id"])
        invoice_id = register(invoice["invoice_id"])
        _identifier(invoice["entity_id"])
        _require(invoice["currency"] == currency, "unsupported_source_currency")
        total = _amount(invoice["total_minor"], positive=True)
        issued, due = _time(invoice["issued_at"]), _time(invoice["due_at"])
        _require(start <= issued < end and due >= issued, "invalid_invoice_dates")
        lines = invoice["line_items"]
        _require(isinstance(lines, list) and bool(lines), "invoice_requires_line_items")
        line_amounts = []
        for line in lines:
            _require(isinstance(line, dict), "invalid_invoice_line")
            amount = _amount(line["total_minor"], positive=True)
            _require(type(line["quantity"]) is int and line["quantity"] > 0, "invalid_invoice_quantity")
            _require(_amount(line["unit_price_minor"], positive=True) * line["quantity"] == amount, "invoice_line_product_mismatch")
            line_amounts.append(amount)
        _require(sum(line_amounts) == total, "invoice_total_mismatch")
        invoices[invoice_id] = invoice
        invoice_lines[invoice_id] = line_amounts

    payments = {}
    known_designations = defaultdict(int)
    for payment in tables["payments"]:
        _require(isinstance(payment, dict), "invalid_payment_record")
        register(payment["doc_id"])
        payment_id = register(payment["payment_id"])
        _require(payment["currency"] == currency, "unsupported_source_currency")
        amount = _amount(payment["amount_minor"], positive=True)
        timestamp = _time(payment["timestamp"])
        _require(start <= timestamp < end, "invalid_payment_date")
        refs = payment["invoice_refs"]
        _require(refs is None or isinstance(refs, list), "invalid_payment_references")
        if refs:
            _require(len(refs) == 1 and refs[0] in invoices, "unsupported_or_unknown_invoice_reference")
            _require(timestamp >= _time(invoices[refs[0]]["issued_at"]), "payment_before_invoice")
            known_designations[refs[0]] += amount
        payments[payment_id] = payment
    for invoice_id, amount in known_designations.items():
        _require(amount <= invoices[invoice_id]["total_minor"], "unsupported_invoice_overpayment")

    recognition = defaultdict(list)
    receipts = defaultdict(list)
    debit_sum = credit_sum = 0
    for row in tables["ledger"]:
        _require(isinstance(row, dict), "invalid_ledger_row")
        register(row["row_id"])
        _identifier(row["entity_id"])
        _require(row["currency"] == currency, "ledger_currency_mismatch")
        _require(row["account"] in accounts.values(), "unsupported_ledger_account")
        debit, credit = _amount(row["debit_minor"]), _amount(row["credit_minor"])
        _require((debit > 0) != (credit > 0), "ledger_row_requires_one_nonzero_side")
        debit_sum += debit
        credit_sum += credit
        _time(row["timestamp"])
        invoice_id, payment_id = row["invoice_id"], row["payment_id"]
        _require(invoice_id is None or invoice_id in invoices, "unknown_ledger_invoice")
        _require(payment_id is None or payment_id in payments, "unknown_ledger_payment")
        if payment_id is None:
            _require(invoice_id is not None, "unattributed_recognition_row")
            recognition[invoice_id].append(row)
        else:
            receipts[payment_id].append(row)
    _require(debit_sum == credit_sum, "unbalanced_general_ledger")

    issues = []
    for invoice_id, invoice in invoices.items():
        rows = recognition[invoice_id]
        primary = [row for row in rows if row["account"] == ar and row["debit_minor"] > 0]
        counterparts = [row for row in rows if row["account"] == revenue and row["credit_minor"] > 0]
        _require(primary and len(primary) + len(counterparts) == len(rows), "invalid_invoice_recognition")
        _require(all(row["entity_id"] == invoice["entity_id"] for row in rows), "invoice_entity_mismatch")
        _require(all(_time(row["timestamp"]) == _time(invoice["issued_at"]) for row in rows), "cutoff_not_supported_in_stage1")
        _require(all(row["debit_minor"] == invoice["total_minor"] for row in primary), "invoice_recognition_amount_mismatch")
        expected_lines = Counter(invoice_lines[invoice_id])
        expected_lines = Counter({amount: count * len(primary) for amount, count in expected_lines.items()})
        _require(Counter(row["credit_minor"] for row in counterparts) == expected_lines, "invoice_counterpart_mismatch")
        _require(sum(row["debit_minor"] for row in rows) == sum(row["credit_minor"] for row in rows), "unbalanced_invoice_journal")
        if len(primary) > 1:
            issues.append(_issue(
                episode_id, "duplicate_invoice", [row["row_id"] for row in primary],
                sum(row["debit_minor"] for row in primary) - invoice["total_minor"], currency,
                ["policy", invoice["doc_id"], *[row["row_id"] for row in primary]],
            ))

    observed_allocations = defaultdict(int)
    for payment_id, payment in payments.items():
        rows = receipts[payment_id]
        bank = [row for row in rows if row["account"] == cash and row["debit_minor"] > 0]
        allocated = [row for row in rows if row["account"] == ar and row["credit_minor"] > 0]
        suspense = [row for row in rows if row["account"] == unallocated and row["credit_minor"] > 0]
        _require(len(bank) == 1 and len(allocated) <= 1 and len(suspense) <= 1, "invalid_receipt_journal")
        _require(len(bank) + len(allocated) + len(suspense) == len(rows), "unsupported_receipt_rows")
        _require(bank[0]["debit_minor"] == payment["amount_minor"], "cash_source_amount_mismatch")
        _require(all(_time(row["timestamp"]) == _time(payment["timestamp"]) for row in rows), "cutoff_not_supported_in_stage1")
        _require(sum(row["debit_minor"] for row in rows) == sum(row["credit_minor"] for row in rows), "unbalanced_receipt_journal")
        _require(len({row["entity_id"] for row in rows}) == 1, "receipt_entity_mismatch")
        for row in allocated:
            _require(row["invoice_id"] is not None, "allocation_requires_invoice_reference")
            observed_allocations[row["invoice_id"]] += row["credit_minor"]
        refs = payment["invoice_refs"]
        if refs:
            invoice_id = refs[0]
            invoice = invoices[invoice_id]
            _require(all(row["entity_id"] == invoice["entity_id"] for row in rows), "payment_entity_mismatch")
            _require(all(row["invoice_id"] == invoice_id for row in allocated), "allocation_invoice_mismatch")
            unallocated_amount = sum(row["credit_minor"] for row in suspense)
            if unallocated_amount:
                issues.append(_issue(
                    episode_id, "payment_allocation", [payment_id, invoice_id],
                    unallocated_amount, currency,
                    ["policy", invoice["doc_id"], payment["doc_id"], *[row["row_id"] for row in rows]],
                ))
        # No intended relation can be invented from entity/date/amount when the
        # source remittance has null/empty invoice_refs. Ledger integrity still
        # gets checked, but such a receipt has no allocation truth.
    for invoice_id, amount in observed_allocations.items():
        _require(amount <= invoices[invoice_id]["total_minor"], "observed_invoice_overallocation")
    return sorted(issues, key=lambda issue: (issue["type"], issue["affected_ids"]))
