"""Post-run causal experiments over a copy of the open masking book.

Only public records are used. Every edited book is reconciled independently;
the original episode, findings and reward are never modified here.
"""

from __future__ import annotations

import copy
from collections import Counter, defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from .checker import reconcile
from .contracts import canonical_json
from .money import convert_minor


class RepairExperimentError(ValueError):
    pass


def _value(public: dict, invoice: dict) -> tuple[int, str, str]:
    manifest = public["manifest"]
    base = manifest["base_currency"]
    day = datetime.fromisoformat(invoice["issued_at"]).astimezone(ZoneInfo(manifest["timezone"])).date().isoformat()
    rate = "1"
    if invoice["currency"] != base:
        rates = [row["rate"] for row in public["fx"] if row["date"] == day
                 and row["source_currency"] == invoice["currency"] and row["target_currency"] == base]
        if len(rates) != 1:
            raise RepairExperimentError("ambiguous_document_date_rate")
        rate = rates[0]
    exponents = manifest["currency_exponents"]
    value = convert_minor(invoice["total_minor"], rate, exponents[invoice["currency"]],
                          exponents[base], rounding=public["policy"]["rounding"])
    return value, rate, day


def _revenue(public: dict) -> int:
    revenue = public["policy"]["accounts"]["revenue"]
    return sum(row["credit_minor"] - row["debit_minor"] for row in public["ledger"] if row["account"] == revenue)


def repair_experiment(public: dict, *, fix_duplicate: bool, fix_fx: bool) -> dict:
    """Return one independently checked variant, never trusting client totals."""
    if type(fix_duplicate) is not bool or type(fix_fx) is not bool:
        raise RepairExperimentError("repair_flags_require_booleans")
    original_json = canonical_json(public)
    before = reconcile(public)
    base = public["manifest"]["base_currency"]
    native = [doc for doc in public["invoices"] if doc["currency"] == base]
    foreign = [doc for doc in public["invoices"] if doc["currency"] != base]
    if (len(public["invoices"]) != 2 or public["payments"] or len(native) != 1 or len(foreign) != 1
            or any(doc["total_minor"] <= 0 for doc in public["invoices"])
            or public["policy"].get("fx_date_rule") != "source_document_date_in_company_timezone"
            or Counter(issue["type"] for issue in before) != {"duplicate_invoice": 1, "fx_conversion": 1}):
        raise RepairExperimentError("requires_open_two_invoice_masking_book")

    book = copy.deepcopy(public)
    accounts = book["policy"]["accounts"]
    if fix_duplicate:
        journals = {row["journal_id"] for row in book["ledger"]
                    if row["invoice_id"] == native[0]["invoice_id"] and row["payment_id"] is None
                    and row["account"] == accounts["receivable"]}
        if len(journals) != 2:
            raise RepairExperimentError("ambiguous_duplicate_journals")
        # Either identical recognition is a valid survivor. Choose by ID so
        # row reordering cannot change the financial outcome.
        keep = min(journals)
        remove = journals - {keep}
        book["ledger"] = [row for row in book["ledger"] if row["journal_id"] not in remove]

    if fix_fx:
        value, rate, day = _value(book, foreign[0])
        for row in book["ledger"]:
            if row["invoice_id"] != foreign[0]["invoice_id"] or row["payment_id"] is not None:
                continue
            if row["account"] == accounts["receivable"]:
                row["debit_minor"], row["credit_minor"] = value, 0
            elif row["account"] == accounts["revenue"]:
                row["debit_minor"], row["credit_minor"] = 0, value
            else:
                raise RepairExperimentError("unsupported_invoice_journal")
            row["applied_fx_rate"], row["applied_fx_date"] = rate, day

    counts = {key: len(book[key]) for key in ("ledger", "invoices", "payments", "fx")}
    book["manifest"].update(ledger_rows=counts["ledger"], document_count=counts["invoices"] + counts["payments"], table_counts=counts)
    after = reconcile(book)
    expected = Counter()
    if not fix_duplicate:
        expected["duplicate_invoice"] = 1
    if not fix_fx:
        expected["fx_conversion"] = 1
    if Counter(issue["type"] for issue in after) != expected:
        raise RepairExperimentError("unexpected_remaining_errors")
    if any(book[key] != public[key] for key in ("invoices", "payments", "fx", "policy")):
        raise RepairExperimentError("source_document_changed")
    journals = defaultdict(list)
    for row in book["ledger"]:
        journals[row["journal_id"]].append(row)
    if any(sum(row["debit_minor"] - row["credit_minor"] for row in rows) != 0 for rows in journals.values()):
        raise RepairExperimentError("unbalanced_edited_journal")

    new_rows = {row["row_id"]: row for row in book["ledger"]}
    changes = []
    for old in public["ledger"]:
        new = new_rows.get(old["row_id"])
        if new == old:
            continue
        changes.append({"row_id": old["row_id"], "account": old["account"], "invoice_id": old["invoice_id"],
                        "operation": "removed" if new is None else "corrected",
                        "before_debit_minor": old["debit_minor"], "before_credit_minor": old["credit_minor"],
                        "after_debit_minor": None if new is None else new["debit_minor"],
                        "after_credit_minor": None if new is None else new["credit_minor"]})
    if canonical_json(public) != original_json:
        raise RepairExperimentError("original_book_changed")
    expected_total = sum(_value(public, invoice)[0] for invoice in public["invoices"])
    return {"scope": "post_run_educational_copy", "currency": base,
            "fixed": {"duplicate_invoice": fix_duplicate, "fx_conversion": fix_fx},
            "before": {"revenue_minor": _revenue(public), "issue_count": len(before)},
            "after": {"revenue_minor": _revenue(book), "issue_count": len(after),
                      "issue_types": sorted(issue["type"] for issue in after)},
            "expected_revenue_minor": expected_total, "total_matches_documents": _revenue(book) == expected_total,
            "source_documents_unchanged": True, "original_episode_unchanged": True,
            "changed_rows": changes, "book": book}
