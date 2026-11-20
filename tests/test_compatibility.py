"""Native version compatibility must retain the strict monetary contract."""

from copy import deepcopy

import pytest

from fin_audit.checker import DataValidationError, reconcile
from fin_audit.generator import generate


@pytest.mark.parametrize("version", ["0.1.0", "0.2.0"])
@pytest.mark.parametrize("exponent", [2.0, True, "2", None])
def test_native_currency_exponent_requires_a_real_integer(version, exponent):
    public = deepcopy(generate(71, {"duplicate_count": 0, "allocation_count": 0}).public)
    public["manifest"]["schema_version"] = version
    public["policy"]["schema_version"] = version
    assert reconcile(public) == []
    public["manifest"]["currency_exponents"]["KZT"] = exponent
    with pytest.raises(DataValidationError, match="unsupported_stage1_currency"):
        reconcile(public)


@pytest.mark.parametrize("manifest_version,policy_version", [("0.1.0", "0.2.0"), ("0.2.0", "0.1.0")])
def test_native_manifest_and_policy_versions_cannot_disagree(manifest_version, policy_version):
    public = deepcopy(generate(72).public)
    public["manifest"]["schema_version"] = manifest_version
    public["policy"]["schema_version"] = policy_version
    with pytest.raises(DataValidationError, match="policy_schema_version_mismatch"):
        reconcile(public)
