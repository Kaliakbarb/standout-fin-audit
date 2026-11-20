"""Standalone public-only reference policies, copied alone into the agent jail.

This file deliberately imports no generator, checker, environment or evaluator.
Its only financial input is the JSONL tool protocol on stdin/stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo


class Finished(Exception):
    pass


def send(action: str, args: dict) -> dict:
    print(json.dumps({"action": action, "args": args}, separators=(",", ":")), flush=True)
    line = sys.stdin.readline()
    if not line:
        raise Finished
    response = json.loads(line)
    if response.get("terminated") or response.get("truncated"):
        # A fresh worker has finished its protocol only after the broker's ACK.
        # Flush failures remain errors; confirmed completion needs no Python
        # interpreter teardown, which can outlast the broker's exit grace.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)
    observation = response["observation"]
    if "error" in observation:
        raise RuntimeError(observation["error"]["code"])
    return observation["result"]


def read_table(table: str) -> list[dict]:
    records: list[dict] = []
    cursor = None
    while True:
        result = send("list_records", {"table": table, "cursor": cursor, "limit": 50})
        records.extend(result["records"])
        cursor = result["next_cursor"]
        if cursor is None:
            return records


def simple_findings(records: dict, policy: dict, currency: str) -> list[dict]:
    """A native-currency baseline, independently written from public rules."""
    accounts = policy["accounts"]
    invoices = {doc["invoice_id"]: doc for doc in records["invoices"]}
    recognitions: dict[str, list[dict]] = defaultdict(list)
    receipts: dict[str, list[dict]] = defaultdict(list)
    for row in records["ledger"]:
        if row["payment_id"] is not None:
            receipts[row["payment_id"]].append(row)
        elif row["invoice_id"] is not None and row["account"] == accounts["receivable"]:
            if row["debit_minor"] > 0 and row["currency"] == currency:
                recognitions[row["invoice_id"]].append(row)
    result = []
    for invoice_id, rows in sorted(recognitions.items()):
        invoice = invoices.get(invoice_id)
        if invoice is None or invoice["currency"] != currency:
            continue
        extra = sum(row["debit_minor"] for row in rows) - invoice["total_minor"]
        if len(rows) > 1 and extra > 0:
            result.append({
                "type": "duplicate_invoice",
                "affected_ids": sorted(row["row_id"] for row in rows),
                "claimed_amount_minor": extra,
                "currency": currency,
                "evidence_ids": sorted({"policy", invoice["doc_id"], *(row["row_id"] for row in rows)}),
                "explanation": "Repeated recognition exceeds the documented invoice total.",
            })
    for payment in sorted(records["payments"], key=lambda row: row["payment_id"]):
        refs = payment["invoice_refs"]
        if payment["currency"] != currency or not isinstance(refs, list) or len(refs) != 1:
            continue
        invoice = invoices.get(refs[0])
        if invoice is None:
            continue
        rows = receipts.get(payment["payment_id"], [])
        unallocated = sum(
            row["credit_minor"] - row["debit_minor"]
            for row in rows if row["account"] == accounts["unallocated"]
        )
        if unallocated > 0 and policy.get("allocation_rule") == "atomic_full_designation":
            result.append({
                "type": "payment_allocation",
                "affected_ids": sorted([payment["payment_id"], invoice["invoice_id"]]),
                "claimed_amount_minor": unallocated,
                "currency": currency,
                "evidence_ids": sorted({
                    "policy", invoice["doc_id"], payment["doc_id"],
                    *(row["row_id"] for row in rows),
                }),
                "explanation": "The documented payment designation leaves an unallocated receipt amount.",
            })
    return result[:30]


def multicurrency_findings(records: dict, policy: dict, manifest: dict) -> list[dict]:
    """Public-only reconstruction of primary valuations and period membership.

    This reference uses source documents and the published policy, independently
    of the private reconciler and the generator's mutation implementation.
    """
    accounts = policy["accounts"]
    base = manifest["base_currency"]
    exponents = manifest["currency_exponents"]
    zone = ZoneInfo(manifest["timezone"])
    start = datetime.fromisoformat(manifest["period_start"])
    end = datetime.fromisoformat(manifest["period_end"])
    invoices = {doc["invoice_id"]: doc for doc in records["invoices"]}
    payments = {doc["payment_id"]: doc for doc in records["payments"]}
    rates = {(row["date"], row["source_currency"], row["target_currency"]): row for row in records["fx"]}
    journals: dict[str, list[dict]] = defaultdict(list)
    recognition: dict[str, list[dict]] = defaultdict(list)
    result = []
    for row in records["ledger"]:
        journals[row["journal_id"]].append(row)

    def finding(kind, affected, amount, evidence, explanation):
        return {"type": kind, "affected_ids": sorted(affected),
                "claimed_amount_minor": amount, "currency": base,
                "evidence_ids": sorted(set(evidence)), "explanation": explanation}

    def membership(timestamp):
        instant = datetime.fromisoformat(timestamp)
        return start <= instant < end

    def expected_valuation(document, amount_key, timestamp_key):
        source = document["currency"]
        amount = document[amount_key]
        fx_evidence = []
        if source == base:
            rate = "1"
        else:
            date = datetime.fromisoformat(document[timestamp_key]).astimezone(zone).date().isoformat()
            fx = rates[date, source, base]
            rate = fx["rate"]
            fx_evidence.append(fx["record_id"])
        with localcontext() as context:
            context.prec = max(80, len(str(abs(amount))) + len(rate) + 30)
            converted = Decimal(amount) * Decimal(rate) * (Decimal(10) ** (exponents[base] - exponents[source]))
            value = abs(int(converted.to_integral_value(rounding=policy["rounding"])))
        return value, fx_evidence

    for journal_id in sorted(journals):
        rows = journals[journal_id]
        first = rows[0]
        payment_id = first["payment_id"]
        if payment_id is None:
            document = invoices[first["invoice_id"]]
            amount_key, timestamp_key = "total_minor", "issued_at"
            side = "debit_minor" if document[amount_key] > 0 else "credit_minor"
            primary = next(row for row in rows if row["account"] == accounts["receivable"] and row[side] > 0)
            if document[amount_key] > 0:
                recognition[document["invoice_id"]].append(primary)
        else:
            document = payments[payment_id]
            amount_key, timestamp_key = "amount_minor", "timestamp"
            side = "debit_minor" if document[amount_key] > 0 else "credit_minor"
            primary = next(row for row in rows if row["account"] == accounts["cash"] and row[side] > 0)
            refs = document["invoice_refs"]
            if document[amount_key] > 0 and isinstance(refs, list) and len(refs) == 1:
                unallocated = sum(row["credit_minor"] - row["debit_minor"] for row in rows if row["account"] == accounts["unallocated"])
                if unallocated > 0 and policy.get("allocation_rule") == "atomic_full_designation":
                    invoice = invoices[refs[0]]
                    result.append(finding(
                        "payment_allocation", [payment_id, invoice["invoice_id"]], unallocated,
                        ["policy", invoice["doc_id"], document["doc_id"], *(row["row_id"] for row in rows)],
                        "The fully designated payment contains an unallocated receipt credit.",
                    ))
        observed = primary["debit_minor"] + primary["credit_minor"]
        expected, fx_evidence = expected_valuation(document, amount_key, timestamp_key)
        if document["currency"] != base and observed != expected:
            result.append(finding(
                "fx_conversion", [primary["row_id"]], abs(observed - expected),
                ["policy", document["doc_id"], primary["row_id"], *fx_evidence],
                "The source amount at the policy date's FX rate differs from its posted base valuation.",
            ))
        if membership(document[timestamp_key]) != membership(primary["timestamp"]):
            result.append(finding(
                "period_cutoff", [primary["row_id"]], observed,
                ["policy", document["doc_id"], primary["row_id"]],
                "Source-document time and the posted journal fall in different reporting periods.",
            ))
    for invoice_id, rows in sorted(recognition.items()):
        if len(rows) > 1:
            # The supported duplicate family repeats the same documented
            # recognition valuation; every original/copy primary is affected.
            retained = rows[0]["debit_minor"]
            result.append(finding(
                "duplicate_invoice", [row["row_id"] for row in rows],
                sum(row["debit_minor"] for row in rows) - retained,
                ["policy", invoices[invoice_id]["doc_id"], *(row["row_id"] for row in rows)],
                "The same source invoice was recognized more than once.",
            ))
    return sorted(result, key=lambda item: (item["type"], item["affected_ids"]))[:30]


def isolation_probe() -> dict[str, bool]:
    """Benign attempts to cross the promised jail; no real external requests."""
    import ctypes
    import socket
    import subprocess

    checks: dict[str, bool] = {}
    for name, path in {
        "private_file_unreadable": "/private/fin_audit_sentinel.json",
        "supervisor_source_unreadable": "/app/src/fin_audit/generator.py",
        "proc_unavailable": "/proc/1/environ",
        "parent_fd_unavailable": f"/proc/{os.getppid()}/fd/0",
    }.items():
        try:
            with open(path, "rb") as handle:
                handle.read(1)
            checks[name] = False
        except OSError:
            checks[name] = True
    try:
        with open("/private/probe-write", "w") as handle:
            handle.write("probe")
        checks["private_write_denied"] = False
    except OSError:
        checks["private_write_denied"] = True
    for name, path in {"worker_code_readonly": "/agent_worker.py", "root_directory_readonly": "/probe-file"}.items():
        try:
            with open(path, "ab") as handle:
                handle.write(b"probe")
            checks[name] = False
        except OSError:
            checks[name] = True
    checks["unprivileged_uid"] = hasattr(os, "getresuid") and os.getresuid() == (65534, 65534, 65534)
    checks["unprivileged_gid"] = hasattr(os, "getresgid") and os.getresgid() == (65534, 65534, 65534)
    checks["no_supplementary_groups"] = os.getgroups() == []
    try:
        os.setresuid(0, 0, 0)
        checks["uid_escalation_denied"] = False
    except OSError:
        checks["uid_escalation_denied"] = True
    checks["root_cwd"] = os.getcwd() == "/"
    checks["private_environment_absent"] = not any(
        "SEED" in key or "SECRET" in key or "SENTINEL" in key for key in os.environ
    )
    inherited = []
    for fd in range(3, 64):
        try:
            os.fstat(fd)
            inherited.append(fd)
        except OSError:
            pass
    checks["only_stdio_descriptors"] = not inherited
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.close()
        checks["network_socket_denied"] = False
    except OSError:
        checks["network_socket_denied"] = True
    try:
        subprocess.run(["/bin/sh", "-c", "exit 0"], check=False, timeout=1)
        checks["shell_unavailable"] = False
    except (OSError, subprocess.SubprocessError):
        checks["shell_unavailable"] = True
    libc = ctypes.CDLL(None, use_errno=True)
    checks["no_new_privileges"] = libc.prctl(39, 0, 0, 0, 0) == 1

    class Header(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]

    class CapData(ctypes.Structure):
        _fields_ = [("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32), ("inheritable", ctypes.c_uint32)]

    caps = (CapData * 2)()
    checks["capabilities_empty"] = libc.capget(ctypes.byref(Header(0x20080522, 0)), ctypes.byref(caps)) == 0 and all(
        field == 0 for cap in caps for field in (cap.effective, cap.permitted, cap.inheritable)
    )
    if checks["unprivileged_uid"] and checks["proc_unavailable"]:
        try:
            child = os.fork()
            if child == 0:
                os._exit(0)
            os.waitpid(child, 0)
            checks["process_creation_denied"] = False
        except OSError:
            checks["process_creation_denied"] = True
    else:
        checks["process_creation_denied"] = False
    return checks


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", choices=["simple", "silent", "random", "mark_all", "probe"], required=True)
    args = parser.parse_args()
    first = sys.stdin.readline()
    if not first:
        return
    start = json.loads(first)["observation"]
    episode_id = start["manifest"]["episode_id"]
    findings: list[dict] = []
    try:
        if args.policy == "probe":
            checks = isolation_probe()
            send("list_records", {"table": "ledger", "limit": 1})
            print("ISOLATION_PROBE=" + json.dumps(checks, sort_keys=True), file=sys.stderr, flush=True)
        elif args.policy == "random":
            rng = random.Random(1701)
            for _ in range(12):
                send("list_records", {"table": rng.choice(["ledger", "invoices", "payments", "fx"]), "limit": rng.randint(1, 10)})
        elif args.policy in ("simple", "mark_all"):
            records = {table: read_table(table) for table in ("ledger", "invoices", "payments", "fx")}
            if args.policy == "simple":
                if start["policy"]["accounting_model"] == "receivables_multi_v2":
                    findings = multicurrency_findings(records, start["policy"], start["manifest"])
                else:
                    findings = simple_findings(records, start["policy"], start["manifest"]["base_currency"])
            else:
                for row in records["ledger"][:30]:
                    findings.append({
                        "type": "duplicate_invoice", "affected_ids": [row["row_id"]],
                        "claimed_amount_minor": row["debit_minor"] + row["credit_minor"],
                        "currency": row["currency"], "evidence_ids": [row["row_id"], "policy"],
                        "explanation": "Shotgun accusation of a public row.",
                    })
        send("submit", {"findings": {"episode_id": episode_id, "findings": findings}})
    except Finished:
        return


if __name__ == "__main__":
    main()
