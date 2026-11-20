"""Accidental labels in new public metadata must fail before observation."""

from copy import deepcopy

import pytest

from fin_audit.contracts import EpisodeBundle, validate_public_privacy
from fin_audit.environment import AuditEnv, EnvironmentStateError
from fin_audit.generator import generate


@pytest.mark.parametrize("location", ["manifest", "policy", "ledger", "line_items"])
@pytest.mark.parametrize("key", ["seed", "truth", "is_error", "expected_amount_minor"])
def test_reserved_authoring_labels_never_reach_a_worker(location, key):
    original = generate(51)
    public = deepcopy(original.public)
    if location in ("manifest", "policy"):
        target = public[location]
    elif location == "ledger":
        target = public["ledger"][0]
    else:
        target = public["invoices"][0]["line_items"][0]
    target[key] = "PRIVATE_AUTHORING_SENTINEL"
    env = AuditEnv(mode="closed")
    env.load_bundle(original)
    with pytest.raises(ValueError, match="^reserved_private_public_key$"):
        env.load_bundle(EpisodeBundle(public, original.truth, original.provenance))
    # A failed replacement cannot leave an earlier episode live for the worker.
    with pytest.raises(EnvironmentStateError, match="reset_required"):
        env.step({"action": "get_document", "args": {"doc_id": "policy"}})


def test_public_descriptions_can_discuss_rules_without_being_hidden_labels():
    validate_public_privacy({"description": "A seed is private; expected amounts are checked.", "source_amount_minor": 19})
