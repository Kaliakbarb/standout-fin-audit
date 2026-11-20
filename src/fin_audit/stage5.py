"""Application readiness; archive transfer is attested by the external runner."""

from __future__ import annotations

import hashlib
import os
import time
import tomllib
from pathlib import Path

from . import __version__
from .checker import reconcile
from .contracts import PROTOCOL_VERSION, SCHEMA_VERSION, canonical_json
from .corpus import PLAN_VERSION
from .profiles import stage2_cases
from .runner import run_episode
from .storage import export_episode, load_episode


def run_release_readiness(*, profile: str, output: Path) -> dict:
    """Verify submission materials and actual workers with optional names."""
    from .presentation import generate_named
    from .web_readiness import run_web_readiness

    root = Path(__file__).resolve().parents[2]
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError("readiness_output_already_exists")
    started = time.monotonic()
    required = ("LICENSE", "README.md", "docs/DESIGN_NOTE.md", "docs/DEMO_GUIDE.md",
                "docs/THIRD_PARTY_NOTICES.md", "pyproject.toml", "uv.lock", "schemas/episode.schema.json",
                "configs/corpus-plan.json", "configs/holdout-plan.json", "configs/scorer-mutations.json",
                "scripts/verify", "scripts/release.py", "scripts/release_support.py", "scripts/clean_rehearsal.py",
                "scripts/demo.py", "docs/WEB_DEMO.md", "src/fin_audit/web.py", "src/fin_audit/web_readiness.py",
                *["src/fin_audit/web_assets/" + name for name in
                  ("index.html", "style.css", "app.js", "lesson.mjs", "replay.mjs")])
    materials = {name: (root / name).is_file() and bool((root / name).read_bytes()) for name in required}
    scenarios = stage2_cases()
    books = {key: generate_named(19, scenarios[case], presentation_seed=501)
             for key, case in (("named_clean", "clean_multicurrency"), ("named_all_four", "all_four"))}
    alternative = generate_named(19, scenarios["all_four"], presentation_seed=502)
    reports = []
    for key, bundle in books.items():
        if reconcile(bundle.public) != bundle.truth:
            raise ValueError("named_book_independent_reconciliation_failed")
        report = run_episode(bundle, policy="simple", mode="closed", profile=profile)
        report["case"] = key
        reports.append(report)
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    checks = {
        "submission_materials_present": all(materials.values()),
        "versions_declared_consistently": version == __version__ and SCHEMA_VERSION == "0.2.0" and PLAN_VERSION == "0.3.0" and PROTOCOL_VERSION == "1.0",
        "synthetic_company_names_vary": books["named_all_four"].public["manifest"]["company_name"] != alternative.public["manifest"]["company_name"],
        "named_public_workers_exact": all(report["evaluation"]["f1"] == 1 and report["evaluation"]["fp"] == 0 and report["evaluation"]["fn"] == 0 for report in reports),
        "named_workers_submit_and_exit": all(report["agent_exitcode"] == 0 and report["evaluation"]["termination_reason"] == "submitted" for report in reports),
        "named_closed_feedback_masked": all(row["reward"] == 0 and row["info"] == {} for report in reports for row in report["trajectory"]),
    }
    # Publish this small developer report only after the whole closed pair ends.
    output.mkdir(parents=True, mode=0o700)
    roundtrips = True
    for key, bundle in books.items():
        export_episode(bundle, output / "episodes" / key)
        roundtrips &= load_episode(output / "episodes" / key) == bundle
    checks["named_books_roundtrip_exact"] = roundtrips
    web = run_web_readiness(profile=profile)
    checks["interactive_demo_http_and_worker_flow"] = web["passed"]
    descriptor = os.open(output / "web-summary.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(canonical_json(web) + "\n")
    compact = [{key: value for key, value in report.items() if key not in {"trajectory", "stderr"}} for report in reports]
    summary = {"software_version": __version__, "scope": "application-and-material-readiness; clean archive transfer is attested externally",
               "profile": profile, "checks": checks, "passed": all(checks.values()), "materials": materials,
               "runs": compact, "elapsed_seconds": time.monotonic() - started,
               "web_readiness": {"path": "web-summary.json", "passed": web["passed"],
                                 "sha256": hashlib.sha256((output / "web-summary.json").read_bytes()).hexdigest()},
               "material_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name, exists in materials.items() if exists}}
    descriptor = os.open(output / "trajectories.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        for index, report in enumerate(reports):
            for row in report["trajectory"]:
                handle.write(canonical_json({"run_id": f"names-{index:02}", "policy": report["policy"], **row}) + "\n")
    descriptor = os.open(output / "summary.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(canonical_json(summary) + "\n")
    return summary
