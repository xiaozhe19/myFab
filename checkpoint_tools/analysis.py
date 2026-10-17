from __future__ import annotations

import statistics

REGULAR = ("Regular_Lot_3", "Regular_Lot_4")


def percentile(values, q=0.95):
    ordered = sorted(values)
    p = (len(ordered) - 1) * q
    lo = int(p)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (p - lo)


def summarize(rows):
    if not rows:
        raise ValueError("Empty measurement window.")
    n = len(rows)
    wip = statistics.mean(r["wip"] for r in rows)
    ins = sum(sum(r["ins"].values()) for r in rows)
    outs = sum(sum(r["outs"].values()) for r in rows)
    ct = {}
    for row in rows:
        for kind, values in row["ct"].items():
            ct.setdefault(kind, []).extend(values)
    slope = (sum((i - (n - 1) / 2) * (r["wip"] - wip) for i, r in enumerate(rows))
             / (n * (n * n - 1) / 12)) if n > 1 else 0.0
    return {
        "days": n, "wip": wip, "input": ins, "output": outs,
        "input_per_day": ins / n, "output_per_day": outs / n,
        "balance_pct": 100 * (outs / ins - 1) if ins else None,
        "drift_pct_per_90_days": 100 * slope * 90 / wip if wip else 0.0,
        "ct": {k: {"n": len(v), "mean": statistics.mean(v), "p95": percentile(v)}
               for k, v in ct.items() if v},
    }


def compare(window, reference, tolerance=0.05):
    errors = {"wip": window["wip"] / reference["wip"] - 1,
              "throughput": window["output_per_day"] / reference["output_per_day"] - 1}
    for kind in REGULAR:
        for metric in ("mean", "p95"):
            errors[kind + "_" + metric] = (
                window["ct"][kind][metric] / reference["ct"][kind][metric] - 1
                if kind in window["ct"] and kind in reference["ct"] else None
            )
    passed = all(value is not None and abs(value) <= tolerance * (2 if key.endswith("_p95") else 1)
                 for key, value in errors.items())
    passed = (passed and window["balance_pct"] is not None
              and abs(window["balance_pct"]) <= tolerance * 100
              and abs(window["drift_pct_per_90_days"]) <= tolerance * 100)
    return {"passed": passed, "relative_errors": errors,
            "balance_pct": window["balance_pct"],
            "drift_pct_per_90_days": window["drift_pct_per_90_days"]}


def assess_rows(rows, tolerance=0.05):
    if len(rows) < 360:
        raise ValueError("At least 360 daily observations are required for certification.")
    reference = summarize(rows[-180:])
    reference_stable = (
        abs(reference["balance_pct"]) <= tolerance * 100
        and abs(reference["drift_pct_per_90_days"]) <= tolerance * 100
    )
    checks = []
    for start in range(0, len(rows) - 89, 30):
        result = compare(summarize(rows[start:start + 90]), reference, tolerance)
        checks.append({"start_day": start, "end_day": start + 90, **result})
    candidate = next((checks[i]["start_day"] for i in range(len(checks) - 2)
                      if all(c["passed"] for c in checks[i:i + 3])), None)
    return {
        "candidate_day": candidate,
        "confirmed_day": candidate + 150 if candidate is not None else None,
        "reference_start_day": len(rows) - 180, "reference_end_day": len(rows),
        "reference_stable": reference_stable, "reference": reference, "checks": checks,
    }
