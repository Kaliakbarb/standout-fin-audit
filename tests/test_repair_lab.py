"""Causal edits must change accounting records and preserve source documents."""

import copy
from collections import defaultdict

import pytest

from fin_audit.checker import reconcile
from fin_audit.contracts import canonical_json
from fin_audit.repair_lab import RepairExperimentError, repair_experiment
from fin_audit.showcase import showcase_cases


@pytest.mark.parametrize("duplicate,fx,total,types,changed", [
    (False, False, 55_000_000, ["duplicate_invoice", "fx_conversion"], 0),
    (True, False, 50_000_000, ["fx_conversion"], 2),
    (False, True, 60_000_000, ["duplicate_invoice"], 2),
    (True, True, 55_000_000, [], 4),
])
def test_four_actual_books_have_expected_totals_independent_errors_and_balanced_entries(duplicate, fx, total, types, changed):
    public = showcase_cases()["masked_both"].public
    original = canonical_json(public)
    report = repair_experiment(public, fix_duplicate=duplicate, fix_fx=fx)
    assert report["before"] == {"revenue_minor": 55_000_000, "issue_count": 2}
    assert report["after"] == {"revenue_minor": total, "issue_count": len(types), "issue_types": types}
    assert report["expected_revenue_minor"] == 55_000_000
    assert report["total_matches_documents"] is (duplicate == fx)
    assert sorted(issue["type"] for issue in reconcile(report["book"])) == types
    assert len(report["changed_rows"]) == changed
    for key in ("invoices", "payments", "fx", "policy"):
        assert report["book"][key] == public[key]
    journals = defaultdict(list)
    for row in report["book"]["ledger"]:
        journals[row["journal_id"]].append(row)
    assert all(sum(row["debit_minor"] - row["credit_minor"] for row in rows) == 0 for rows in journals.values())
    assert canonical_json(public) == original


def test_repair_is_financially_invariant_to_public_row_order():
    public = showcase_cases()["masked_both"].public
    reordered = copy.deepcopy(public)
    reordered["ledger"].reverse()
    first = repair_experiment(public, fix_duplicate=True, fix_fx=True)
    second = repair_experiment(reordered, fix_duplicate=True, fix_fx=True)
    assert first["after"] == second["after"]
    assert sorted(first["book"]["ledger"], key=lambda row: row["row_id"]) == sorted(second["book"]["ledger"], key=lambda row: row["row_id"])


@pytest.mark.parametrize("bad", [0, 1, None, "true", [], {}])
def test_flags_do_not_coerce_untrusted_types(bad):
    with pytest.raises(RepairExperimentError, match="booleans"):
        repair_experiment(showcase_cases()["masked_both"].public, fix_duplicate=bad, fix_fx=False)


@pytest.mark.parametrize("case", ["clean", "duplicate_only", "fx_only", "allocation_fx"])
def test_other_books_do_not_receive_invented_masking_experiments(case):
    with pytest.raises(RepairExperimentError, match="masking_book"):
        repair_experiment(showcase_cases()[case].public, fix_duplicate=True, fix_fx=True)
