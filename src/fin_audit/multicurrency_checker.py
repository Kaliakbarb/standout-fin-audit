"""Independent reconciliation of the public multi-currency receivables model.

No generator, provenance, mutation, or proposed-answer input is consulted. Every
journal is validated before the complete supported issue set is returned.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .checker_errors import DataValidationError
from .contracts import canonical_json
from .money import convert_minor, currency_exponent, minor_integer

_RATE = re.compile(r"[+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\Z")
_TABLES = ("ledger", "invoices", "payments", "fx")
_METADATA = (
    "source_currency", "source_amount_minor", "applied_fx_date",
    "applied_fx_rate", "allocated_minor",
)
_POLICY_RULES = {
    "accounting_model": "receivables_multi_v2",
    "allocation_rule": "atomic_full_designation",
    "period_bounds": "[start,end)",
    "invoice_recognition": "issued_at",
    "payment_recognition": "timestamp",
    "amount_currency": "base_currency",
    "receipt_lifecycle": "one_atomic_journal_no_later_clearing",
    "unknown_payment_designation": "do_not_infer_an_invoice_from_entity_date_or_amount",
    "fx_date_rule": "source_document_date_in_company_timezone",
    "fx_rate_convention": "target_major_per_source_major",
    "carrying_rule": "correct_invoice_source_date_rate",
    "fx_balance_rule": "cash_minus_carrying_minus_unallocated",
    "refund_rule": "native_credit_note_full_designation",
    "source_amount_rule": "whole_document_amount_per_journal",
}
_CANONICAL_RULES = {
    "duplicate_invoice": "all_receivable_debit_recognition_row_ids_including_original",
    "payment_allocation": "payment_id_and_invoice_id",
    "fx_conversion": "primary_ledger_row_id",
    "period_cutoff": "primary_ledger_row_id",
}
_AMOUNT_RULES = {
    "duplicate_invoice": "excess_gross_recognition_in_base_minor_units",
    "payment_allocation": "excess_unallocated_receipt_credit_in_observed_valuation",
    "fx_conversion": "absolute_primary_valuation_difference_in_base_minor_units",
    "period_cutoff": "absolute_primary_amount_in_base_minor_units",
}


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise DataValidationError(code)


def _id(value: Any) -> str:
    _require(type(value) is str and 0 < len(value) <= 200, "invalid_record_id")
    return value


def _money(value: Any, *, signed: bool = False, nonzero: bool = False) -> int:
    try:
        result = minor_integer(value, nonnegative=not signed)
    except ValueError as error:
        raise DataValidationError(str(error)) from error
    _require(not nonzero or result != 0, "source_amount_must_be_nonzero")
    return result


def _time(value: Any) -> datetime:
    _require(type(value) is str, "timestamp_requires_string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise DataValidationError("invalid_iso_timestamp") from error
    _require(parsed.tzinfo is not None and parsed.utcoffset() is not None, "naive_timestamp")
    return parsed


def _date(value: Any) -> str:
    _require(type(value) is str and len(value) == 10, "invalid_fx_date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise DataValidationError("invalid_fx_date") from error
    _require(parsed.isoformat() == value, "invalid_fx_date")
    return value


def _rate(value: Any) -> Decimal:
    _require(type(value) is str and len(value) <= 100 and _RATE.fullmatch(value) is not None, "invalid_fx_rate")
    parsed = Decimal(value)
    _require(parsed.is_finite() and parsed > 0, "invalid_fx_rate")
    return parsed


def _issue(episode: str, kind: str, affected: list[str], amount: int, evidence: list[str]) -> dict:
    affected = sorted(set(affected))
    evidence = sorted(set(evidence))
    _require(0 < len(evidence) <= 10, "evidence_budget_exceeded")
    _require(amount > 0, "zero_effect_is_not_an_issue")
    identity = canonical_json([episode, kind, affected])
    return {
        "issue_id": "issue_" + hashlib.sha256(identity.encode()).hexdigest()[:24],
        "type": kind,
        "affected_ids": affected,
        "expected_amount_minor": amount,
        "currency": "KZT",
        "evidence_sets": [evidence],
    }


class _Book:
    def __init__(self, public: dict, policy: dict | None) -> None:
        self.public = public
        self.manifest = public["manifest"]
        self.policy = public["policy"]
        _require(type(self.manifest) is dict and type(self.policy) is dict, "invalid_manifest_policy")
        if policy is not None:
            _require(policy == self.policy, "policy_does_not_match_public_book")
        _require(self.manifest.get("schema_version") == self.policy.get("schema_version") == "0.2.0", "unsupported_schema_version")
        for key, value in _POLICY_RULES.items():
            _require(self.policy.get(key) == value, "unsupported_" + key)
        _require(self.policy.get("canonical_rules") == _CANONICAL_RULES, "unsupported_canonical_rules")
        _require(self.policy.get("amount_rules") == _AMOUNT_RULES, "unsupported_amount_rules")
        _require(type(self.policy.get("amount_tolerance_minor")) is int and self.policy["amount_tolerance_minor"] == 0, "unsupported_amount_tolerance")
        self.rounding = self.policy.get("rounding")
        _require(self.rounding in {"ROUND_HALF_EVEN", "ROUND_HALF_UP", "ROUND_DOWN"}, "unsupported_rounding_policy")
        _require(self.manifest.get("rounding") == self.rounding, "rounding_policy_mismatch")
        self.accounts = self.policy["accounts"]
        account_keys = {"receivable", "revenue", "cash", "unallocated", "fx_gain", "fx_loss"}
        _require(type(self.accounts) is dict and set(self.accounts) == account_keys, "invalid_account_registry")
        _require(all(type(value) is str and value for value in self.accounts.values()), "invalid_account_name")
        _require(len(set(self.accounts.values())) == 6, "ambiguous_account_registry")
        _require(self.manifest.get("base_currency") == "KZT", "unsupported_base_currency")
        self.exponents = self.manifest["currency_exponents"]
        _require(type(self.exponents) is dict and self.exponents.get("KZT") == 2, "invalid_currency_registry")
        for name, exponent in self.exponents.items():
            _id(name)
            try:
                currency_exponent(exponent)
            except ValueError as error:
                raise DataValidationError(str(error)) from error
        self.episode = _id(self.manifest["episode_id"])
        self.start, self.end = _time(self.manifest["period_start"]), _time(self.manifest["period_end"])
        _require(self.start < self.end, "empty_reporting_period")
        try:
            self.timezone = ZoneInfo(self.manifest["timezone"])
        except (ValueError, TypeError, ZoneInfoNotFoundError) as error:
            raise DataValidationError("invalid_company_timezone") from error
        for name in _TABLES:
            _require(type(public[name]) is list and all(type(row) is dict for row in public[name]), "invalid_public_table")
        expected_counts = {name: len(public[name]) for name in _TABLES}
        _require(type(self.manifest.get("ledger_rows")) is int and self.manifest["ledger_rows"] == expected_counts["ledger"], "ledger_count_mismatch")
        _require(type(self.manifest.get("document_count")) is int and self.manifest["document_count"] == expected_counts["invoices"] + expected_counts["payments"], "document_count_mismatch")
        _require(self.manifest.get("table_counts") == expected_counts, "table_counts_mismatch")
        _require(all(type(value) is int for value in self.manifest["table_counts"].values()), "invalid_table_count_type")
        _require(expected_counts["ledger"] <= 2000 and expected_counts["invoices"] + expected_counts["payments"] <= 200 and expected_counts["fx"] <= 200, "supported_record_budget_exceeded")
        self.ids = {"policy", self.episode}
        self.fx: dict[tuple[str, str, str], dict] = {}
        self.invoices: dict[str, dict] = {}
        self.invoice_lines: dict[str, list[int]] = {}
        self.payments: dict[str, dict] = {}
        self.journals: dict[str, list[dict]] = defaultdict(list)
        self.recognitions: dict[str, list[list[dict]]] = defaultdict(list)
        self.receipts: dict[str, list[list[dict]]] = defaultdict(list)
        self.issues: list[dict] = []

    def register(self, identity: Any) -> str:
        identity = _id(identity)
        _require(identity not in self.ids, "duplicate_public_identifier")
        self.ids.add(identity)
        return identity

    def currency(self, value: Any) -> str:
        _require(type(value) is str and value in self.exponents, "unsupported_source_currency")
        return value

    def source_date(self, timestamp: str) -> str:
        return _time(timestamp).astimezone(self.timezone).date().isoformat()

    def lookup(self, day: str, currency: str) -> tuple[str, str | None]:
        if currency == "KZT":
            return "1", None
        record = self.fx.get((day, currency, "KZT"))
        _require(record is not None, "missing_fx_rate")
        return record["rate"], record["record_id"]

    def converted(self, amount: int, currency: str, rate: str) -> int:
        try:
            return convert_minor(amount, rate, self.exponents[currency], self.exponents["KZT"], rounding=self.rounding)
        except ValueError as error:
            raise DataValidationError(str(error)) from error

    def fx_records(self) -> None:
        for record in self.public["fx"]:
            self.register(record["record_id"])
            day = _date(record["date"])
            source, target = self.currency(record["source_currency"]), self.currency(record["target_currency"])
            _require(source != "KZT" and target == "KZT", "unsupported_fx_pair")
            _rate(record["rate"])
            key = (day, source, target)
            _require(key not in self.fx, "duplicate_fx_key")
            self.fx[key] = record

    def documents(self) -> None:
        credits: dict[str, int] = defaultdict(int)
        for invoice in self.public["invoices"]:
            self.register(invoice["doc_id"])
            identity = self.register(invoice["invoice_id"])
            _id(invoice["entity_id"])
            currency = self.currency(invoice["currency"])
            total = _money(invoice["total_minor"], signed=True, nonzero=True)
            issued, due = _time(invoice["issued_at"]), _time(invoice["due_at"])
            _require(due >= issued, "invalid_invoice_dates")
            lines = invoice["line_items"]
            _require(type(lines) is list and bool(lines), "invoice_requires_line_items")
            amounts = []
            for line in lines:
                _require(type(line) is dict, "invalid_invoice_line")
                quantity = line["quantity"]
                _require(type(quantity) is int and quantity > 0, "invalid_invoice_quantity")
                unit = _money(line["unit_price_minor"], signed=True, nonzero=True)
                amount = _money(line["total_minor"], signed=True, nonzero=True)
                _require(unit * quantity == amount and (amount > 0) == (total > 0), "invoice_line_product_mismatch")
                amounts.append(amount)
            _require(sum(amounts) == total, "invoice_total_mismatch")
            _require(currency == "KZT" or len(amounts) == 1, "unsupported_foreign_line_rounding")
            original = invoice.get("original_invoice_id")
            if total < 0:
                _require(currency == "KZT" and original is not None and len(amounts) == 1, "unsupported_credit_note")
                _id(original)
                credits[original] += -total
            else:
                _require(original is None, "positive_invoice_has_credit_reference")
            self.invoices[identity] = invoice
            self.invoice_lines[identity] = amounts
            self.lookup(self.source_date(invoice["issued_at"]), currency)
        for invoice in self.invoices.values():
            original_id = invoice.get("original_invoice_id")
            if invoice["total_minor"] < 0:
                original = self.invoices.get(original_id)
                _require(original is not None and original["total_minor"] > 0 and original["currency"] == "KZT", "invalid_credit_note_reference")
                _require(invoice["entity_id"] == original["entity_id"], "credit_note_entity_mismatch")
                _require(_time(invoice["issued_at"]) >= _time(original["issued_at"]), "credit_note_before_original")
                _require(credits[original_id] <= original["total_minor"], "credit_notes_exceed_original")
        designated: dict[str, int] = defaultdict(int)
        for payment in self.public["payments"]:
            self.register(payment["doc_id"])
            identity = self.register(payment["payment_id"])
            currency = self.currency(payment["currency"])
            amount = _money(payment["amount_minor"], signed=True, nonzero=True)
            timestamp = _time(payment["timestamp"])
            refs = payment["invoice_refs"]
            _require(refs is None or type(refs) is list, "invalid_payment_references")
            if refs:
                _require(len(refs) == 1 and type(refs[0]) is str and refs[0] in self.invoices, "unsupported_or_unknown_invoice_reference")
                invoice = self.invoices[refs[0]]
                _require(invoice["currency"] == currency, "cross_currency_settlement_unsupported")
                _require((amount > 0) == (invoice["total_minor"] > 0), "payment_document_direction_mismatch")
                _require(timestamp >= _time(invoice["issued_at"]), "payment_before_invoice")
                designated[refs[0]] += amount
            if amount < 0:
                _require(currency == "KZT" and refs, "unsupported_refund")
            self.payments[identity] = payment
            self.lookup(self.source_date(payment["timestamp"]), currency)
        for invoice_id, amount in designated.items():
            _require(abs(amount) <= abs(self.invoices[invoice_id]["total_minor"]), "unsupported_invoice_overpayment")

    def ledger(self) -> None:
        total_debit = total_credit = 0
        for row in self.public["ledger"]:
            self.register(row["row_id"])
            _id(row["entity_id"])
            _require(row["currency"] == "KZT", "ledger_currency_mismatch")
            _require(row["account"] in self.accounts.values(), "unsupported_ledger_account")
            debit, credit = _money(row["debit_minor"]), _money(row["credit_minor"])
            _require((debit > 0) != (credit > 0), "ledger_row_requires_one_nonzero_side")
            total_debit += debit
            total_credit += credit
            _time(row["timestamp"])
            journal = _id(row["journal_id"])
            _require(journal not in self.ids, "journal_record_identifier_collision")
            self.currency(row["source_currency"])
            _money(row["source_amount_minor"], signed=True, nonzero=True)
            _date(row["applied_fx_date"])
            _rate(row["applied_fx_rate"])
            _require(row["allocated_minor"] is None or type(row["allocated_minor"]) is int, "invalid_allocated_source_amount")
            invoice_id, payment_id = row["invoice_id"], row["payment_id"]
            _require(invoice_id is None or invoice_id in self.invoices, "unknown_ledger_invoice")
            _require(payment_id is None or payment_id in self.payments, "unknown_ledger_payment")
            if invoice_id is not None:
                _require(row["entity_id"] == self.invoices[invoice_id]["entity_id"], "ledger_invoice_entity_mismatch")
                if payment_id is not None:
                    _require(self.invoices[invoice_id]["currency"] == self.payments[payment_id]["currency"], "cross_currency_settlement_unsupported")
            self.journals[journal].append(row)
        _require(total_debit == total_credit, "unbalanced_general_ledger")
        _require(not set(self.journals).intersection(self.ids), "journal_record_identifier_collision")
        for journal_id, rows in self.journals.items():
            # Metadata describe the observed posting, never the hidden answer.
            first = rows[0]
            for row in rows:
                _require(all(row[key] == first[key] for key in _METADATA if key != "applied_fx_rate") and _rate(row["applied_fx_rate"]) == _rate(first["applied_fx_rate"]), "inconsistent_journal_metadata")
                _require(_time(row["timestamp"]) == _time(first["timestamp"]), "inconsistent_journal_timestamp")
                _require(row["entity_id"] == first["entity_id"], "inconsistent_journal_entity")
                _require(row["payment_id"] == first["payment_id"], "mixed_journal_payment")
            _require(sum(row["debit_minor"] for row in rows) == sum(row["credit_minor"] for row in rows), "unbalanced_journal")
            payment_id = first["payment_id"]
            if payment_id is None:
                invoice_id = first["invoice_id"]
                _require(invoice_id is not None and all(row["invoice_id"] == invoice_id for row in rows), "mixed_journal_invoice")
                self.recognitions[invoice_id].append(rows)
            else:
                self.receipts[payment_id].append(rows)

    def event(self, rows: list[dict], document: dict, *, payment: bool) -> tuple[dict, str]:
        first = rows[0]
        amount_key, time_key = ("amount_minor", "timestamp") if payment else ("total_minor", "issued_at")
        _require(first["source_currency"] == document["currency"], "journal_source_currency_mismatch")
        _require(first["source_amount_minor"] == document[amount_key], "journal_source_amount_mismatch")
        if document["currency"] == "KZT":
            _require(_rate(first["applied_fx_rate"]) == 1, "native_fx_rate_must_be_identity")
        else:
            self.lookup(first["applied_fx_date"], document["currency"])
        primary_account = self.accounts["cash" if payment else "receivable"]
        positive = document[amount_key] > 0
        primary = [row for row in rows if row["account"] == primary_account and (row["debit_minor"] > 0) == positive]
        _require(len(primary) == 1, "invalid_event_primary_row")
        primary = primary[0]
        signed_observed = primary["debit_minor"] - primary["credit_minor"]
        observed_expected = self.converted(document[amount_key], document["currency"], first["applied_fx_rate"])
        _require(observed_expected != 0 and signed_observed == observed_expected, "observed_valuation_metadata_mismatch")
        correct_rate, correct_id = self.lookup(self.source_date(document[time_key]), document["currency"])
        normative = self.converted(document[amount_key], document["currency"], correct_rate)
        delta = abs(signed_observed - normative)
        if delta:
            _require(correct_id is not None, "unsupported_native_valuation_error")
            self.issues.append(_issue(self.episode, "fx_conversion", [primary["row_id"]], delta, ["policy", document["doc_id"], primary["row_id"], correct_id]))
        source_inside = self.start <= _time(document[time_key]) < self.end
        observed_inside = self.start <= _time(first["timestamp"]) < self.end
        if source_inside != observed_inside:
            self.issues.append(_issue(self.episode, "period_cutoff", [primary["row_id"]], abs(signed_observed), ["policy", document["doc_id"], primary["row_id"]]))
        return primary, correct_rate

    def invoice_journals(self) -> None:
        for invoice_id, invoice in self.invoices.items():
            journals = self.recognitions[invoice_id]
            _require(bool(journals), "missing_invoice_journal")
            _require(invoice["total_minor"] > 0 or len(journals) == 1, "duplicate_credit_note_unsupported")
            primaries = []
            for rows in journals:
                primary, _ = self.event(rows, invoice, payment=False)
                primaries.append(primary)
                first = rows[0]
                _require(first["allocated_minor"] is None, "invoice_has_payment_allocation")
                _require(first["entity_id"] == invoice["entity_id"], "invoice_entity_mismatch")
                positive = invoice["total_minor"] > 0
                counterparts = [row for row in rows if row["account"] == self.accounts["revenue"] and (row["credit_minor"] > 0) == positive]
                _require(len(counterparts) + 1 == len(rows), "unsupported_recognition_rows")
                expected = Counter(abs(self.converted(amount, invoice["currency"], first["applied_fx_rate"])) for amount in self.invoice_lines[invoice_id])
                observed = Counter(row["credit_minor"] if positive else row["debit_minor"] for row in counterparts)
                _require(expected == observed, "invoice_counterpart_mismatch")
            if len(primaries) > 1:
                values = {row["debit_minor"] for row in primaries}
                _require(len(values) == 1, "mixed_duplicate_valuation_unsupported")
                amount = primaries[0]["debit_minor"] * (len(primaries) - 1)
                self.issues.append(_issue(self.episode, "duplicate_invoice", [row["row_id"] for row in primaries], amount, ["policy", invoice["doc_id"], *[row["row_id"] for row in primaries]]))

    def payment_journals(self) -> None:
        observed_allocations: dict[str, int] = defaultdict(int)
        for payment_id, payment in self.payments.items():
            journals = self.receipts[payment_id]
            _require(len(journals) == 1, "invalid_receipt_journal_count")
            rows = journals[0]
            primary, _ = self.event(rows, payment, payment=True)
            amount, allocated = payment["amount_minor"], rows[0]["allocated_minor"]
            _require(type(allocated) is int, "payment_requires_allocated_source_amount")
            if amount > 0:
                _require(0 <= allocated <= amount, "allocation_source_out_of_range")
            else:
                _require(allocated == amount, "refund_must_be_fully_designated")
            refs = payment["invoice_refs"]
            ar_rows = [row for row in rows if row["account"] == self.accounts["receivable"]]
            _require(len(ar_rows) == (1 if allocated else 0), "invalid_allocation_rows")
            invoice_id = refs[0] if refs else (ar_rows[0]["invoice_id"] if ar_rows else None)
            invoice = self.invoices.get(invoice_id) if invoice_id is not None else None
            if allocated:
                _require(invoice is not None and invoice["currency"] == payment["currency"], "allocation_invoice_mismatch")
                _require(ar_rows[0]["invoice_id"] == invoice_id, "allocation_invoice_mismatch")
                _require((allocated > 0) == (invoice["total_minor"] > 0), "allocation_direction_mismatch")
                _require(_time(payment["timestamp"]) >= _time(invoice["issued_at"]), "allocation_before_invoice")
                observed_allocations[invoice_id] += allocated
            if invoice is not None:
                _require(all(row["entity_id"] == invoice["entity_id"] for row in rows), "payment_entity_mismatch")
                _require(all(row["invoice_id"] is None or row["invoice_id"] == invoice_id for row in rows), "receipt_invoice_link_mismatch")
            if allocated:
                invoice_rate, _ = self.lookup(self.source_date(invoice["issued_at"]), invoice["currency"])
                carrying = self.converted(allocated, payment["currency"], invoice_rate)
            else:
                carrying = 0
            suspense = self.converted(amount - allocated, payment["currency"], rows[0]["applied_fx_rate"])
            cash = primary["debit_minor"] - primary["credit_minor"]
            gain = cash - carrying - suspense
            expected = Counter()

            def expected_row(account: str, debit: int, credit: int) -> None:
                if debit or credit:
                    expected[(self.accounts[account], debit, credit)] += 1

            expected_row("cash", max(cash, 0), max(-cash, 0))
            expected_row("receivable", max(-carrying, 0), max(carrying, 0))
            expected_row("unallocated", 0, suspense)
            expected_row("fx_gain", 0, max(gain, 0))
            expected_row("fx_loss", max(-gain, 0), 0)
            observed = Counter((row["account"], row["debit_minor"], row["credit_minor"]) for row in rows)
            _require(expected == observed, "receipt_counterpart_mismatch")
            if refs and amount > 0 and suspense > 0:
                self.issues.append(_issue(self.episode, "payment_allocation", [payment_id, invoice_id], suspense, ["policy", invoice["doc_id"], payment["doc_id"], *[row["row_id"] for row in rows]]))
        for invoice_id, amount in observed_allocations.items():
            _require(abs(amount) <= abs(self.invoices[invoice_id]["total_minor"]), "observed_invoice_overallocation")

    def reconcile(self) -> list[dict]:
        self.fx_records()
        self.documents()
        self.ledger()
        self.invoice_journals()
        self.payment_journals()
        return sorted(self.issues, key=lambda issue: (issue["type"], issue["affected_ids"]))


def reconcile(public: dict[str, Any], policy: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    try:
        _require(type(public) is dict, "public_book_requires_object")
        return _Book(public, policy).reconcile()
    except DataValidationError:
        raise
    except (KeyError, TypeError, IndexError, AttributeError, ValueError) as error:
        raise DataValidationError("malformed_public_book") from error
