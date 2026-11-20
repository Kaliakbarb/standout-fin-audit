"""Independent, hand-specified public financial examples used by G2 tests."""

from __future__ import annotations

import copy
from datetime import datetime
from zoneinfo import ZoneInfo

from fin_audit.generator import generate
from fin_audit.profiles import stage2_cases


def update_counts(public):
    counts = {name: len(public[name]) for name in ("ledger", "invoices", "payments", "fx")}
    public["manifest"].update(
        ledger_rows=counts["ledger"], document_count=counts["invoices"] + counts["payments"],
        table_counts=counts,
    )


def _template():
    template = generate(70, stage2_cases()["clean_multicurrency"]).public
    return {
        "manifest": copy.deepcopy(template["manifest"]),
        "policy": copy.deepcopy(template["policy"]),
        "ledger": [], "invoices": [], "payments": [], "fx": [],
    }


def fx_example(*, corrected=False, fully_allocated=False):
    """Hand-specified $1000@500 invoice and $600@495/505 receipt.

    KZT and USD both use exponent 2. Thus 49,500 KZT is 4,950,000 minor units,
    not 49,500 minor units. Expected values below are arithmetic fixtures,
    independent of generator mutation formulas and private provenance.
    """
    public = _template()
    public["manifest"].update(
        episode_id="independent-fx-example", currency_exponents={"KZT": 2, "USD": 2},
        period_start="2026-11-01T00:00:00+05:00", period_end="2026-12-01T00:00:00+05:00",
        timezone="Asia/Almaty",
    )
    invoice = {
        "doc_id": "invoice-document", "invoice_id": "business-invoice", "entity_id": "entity",
        "issued_at": "2026-11-10T12:00:00+05:00", "due_at": "2026-12-10T12:00:00+05:00",
        "currency": "USD", "total_minor": 100_000,
        "line_items": [{"description": "Documented service", "quantity": 1,
                        "unit_price_minor": 100_000, "total_minor": 100_000}],
    }
    payment = {
        "doc_id": "payment-document", "payment_id": "business-payment",
        "timestamp": "2026-11-20T12:00:00+05:00", "currency": "USD",
        "amount_minor": 60_000, "invoice_refs": [invoice["invoice_id"]],
    }
    public["invoices"], public["payments"] = [invoice], [payment]
    public["fx"] = [
        {"record_id": "invoice-rate", "date": "2026-11-10", "source_currency": "USD",
         "target_currency": "KZT", "rate": "500"},
        {"record_id": "payment-rate", "date": "2026-11-20", "source_currency": "USD",
         "target_currency": "KZT", "rate": "505"},
    ]
    accounts = public["policy"]["accounts"]

    def row(identity, account, debit, credit, *, receipt=False, no_invoice=False):
        return {
            "row_id": identity, "timestamp": payment["timestamp"] if receipt else invoice["issued_at"],
            "account": accounts[account], "debit_minor": debit, "credit_minor": credit,
            "currency": "KZT", "entity_id": "entity",
            "invoice_id": None if no_invoice else invoice["invoice_id"],
            "payment_id": payment["payment_id"] if receipt else None,
            "journal_id": "payment-journal" if receipt else "invoice-journal",
            "source_currency": "USD", "source_amount_minor": 60_000 if receipt else 100_000,
            "applied_fx_date": "2026-11-20" if receipt else "2026-11-10",
            "applied_fx_rate": ("505" if corrected else "495") if receipt else "500",
            "allocated_minor": (60_000 if fully_allocated else 50_000) if receipt else None,
        }

    ledger = [row("invoice-ar", "receivable", 50_000_000, 0),
              row("invoice-revenue", "revenue", 0, 50_000_000)]
    if fully_allocated:
        assert corrected
        ledger += [row("cash", "cash", 30_300_000, 0, receipt=True),
                   row("allocation", "receivable", 0, 30_000_000, receipt=True),
                   row("fx-component", "fx_gain", 0, 300_000, receipt=True)]
    elif corrected:
        ledger += [row("cash", "cash", 30_300_000, 0, receipt=True),
                   row("allocation", "receivable", 0, 25_000_000, receipt=True),
                   row("unallocated", "unallocated", 0, 5_050_000, receipt=True, no_invoice=True),
                   row("fx-component", "fx_gain", 0, 250_000, receipt=True)]
    else:
        ledger += [row("cash", "cash", 29_700_000, 0, receipt=True),
                   row("allocation", "receivable", 0, 25_000_000, receipt=True),
                   row("unallocated", "unallocated", 0, 4_950_000, receipt=True, no_invoice=True),
                   row("fx-component", "fx_loss", 250_000, 0, receipt=True)]
    public["ledger"] = ledger
    update_counts(public)
    return public


def boundary_example(source_timestamp, recorded_timestamp):
    public = _template()
    public["manifest"].update(
        episode_id="independent-boundary-example", currency_exponents={"KZT": 2},
        period_start="2026-11-01T00:00:00+05:00", period_end="2026-12-01T00:00:00+05:00",
        timezone="Asia/Almaty",
    )
    source = datetime.fromisoformat(source_timestamp)
    invoice = {
        "doc_id": "invoice-document", "invoice_id": "business-invoice", "entity_id": "entity",
        "issued_at": source_timestamp, "due_at": "2026-12-31T12:00:00+05:00",
        "currency": "KZT", "total_minor": 100_000,
        "line_items": [{"description": "Boundary service", "quantity": 1,
                        "unit_price_minor": 100_000, "total_minor": 100_000}],
    }
    public["invoices"] = [invoice]
    common = {
        "timestamp": recorded_timestamp, "currency": "KZT", "entity_id": "entity",
        "invoice_id": invoice["invoice_id"], "payment_id": None, "journal_id": "journal",
        "source_currency": "KZT", "source_amount_minor": 100_000,
        "applied_fx_date": source.astimezone(ZoneInfo("Asia/Almaty")).date().isoformat(),
        "applied_fx_rate": "1", "allocated_minor": None,
    }
    accounts = public["policy"]["accounts"]
    public["ledger"] = [
        {**common, "row_id": "primary", "account": accounts["receivable"], "debit_minor": 100_000, "credit_minor": 0},
        {**common, "row_id": "counterpart", "account": accounts["revenue"], "debit_minor": 0, "credit_minor": 100_000},
    ]
    update_counts(public)
    return public
