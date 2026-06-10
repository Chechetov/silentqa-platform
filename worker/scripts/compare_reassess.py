"""
Compare original quality.json vs quality_v2.json across all sessions.

Deterministic analyzer (no LLM calls). Highlights calls where the reassess
produced materially different or richer output — specifically:
- New meeting_argumentation_assessment with weak push and missed opportunities
- Overall score diverged by >=2
- New plan's talking_points are tagged with playbook category_ids

Usage (from /root/projects/realestate/worker):
    ../.venv/bin/python3 -m scripts.compare_reassess [--top 20]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from prompts.sales_playbook import MEETING_PLAYBOOK_CATEGORIES  # noqa: E402


RESULTS_PATH = Path(os.getenv("RESULTS_STORAGE_PATH", "/data/realestate/results"))
PLAYBOOK_SET = set(MEETING_PLAYBOOK_CATEGORIES)


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _score_interestingness(old: dict, new: dict, new_plan: dict | None) -> tuple[int, list[str]]:
    """Heuristic score + list of reasons why this call is 'interesting' after reassess."""
    score = 0
    reasons: list[str] = []

    maa = new.get("meeting_argumentation_assessment") or {}
    push = maa.get("overall_push_quality")
    missed = maa.get("missed_opportunities") or []
    args_used = maa.get("arguments_used") or []

    # Actionable coaching: weak push with concrete missed opportunities
    if push == "weak" and missed:
        score += 3 + min(len(missed), 4)
        reasons.append(f"weak push + {len(missed)} missed opportunities")
    elif push == "adequate" and missed:
        score += 2
        reasons.append(f"adequate push, {len(missed)} missed opportunities")
    elif push == "strong":
        reasons.append("strong push")
        score += 1
    elif push == "not_applicable":
        # Not inherently interesting; skip
        pass

    # Score divergence between old and new overall_score
    old_score = old.get("overall_score")
    new_score = new.get("overall_score")
    if isinstance(old_score, int) and isinstance(new_score, int):
        delta = new_score - old_score
        if abs(delta) >= 2:
            score += 3
            reasons.append(f"overall_score {old_score} → {new_score} (Δ{delta:+d})")
        elif abs(delta) == 1:
            score += 1
            reasons.append(f"overall_score {old_score} → {new_score}")

    # New plan uses playbook categories in talking_points.topic
    if new_plan:
        tps = new_plan.get("talking_points") or []
        playbook_tagged = [tp for tp in tps if tp.get("topic") in PLAYBOOK_SET]
        if playbook_tagged:
            score += 2
            reasons.append(f"{len(playbook_tagged)}/{len(tps)} talking_points tagged with playbook ids")

    # Args actually used (broker did something right)
    if args_used:
        reasons.append(f"{len(args_used)} playbook-mapped arguments used by broker")

    return score, reasons


def analyze() -> dict:
    sessions = sorted(p.name for p in RESULTS_PATH.iterdir() if p.is_dir())
    rows: list[dict] = []

    counts = {
        "total_dirs": 0,
        "has_v2": 0,
        "missing_v2": 0,
        "too_short": 0,
        "weak_push": 0,
        "adequate_push": 0,
        "strong_push": 0,
        "not_applicable_push": 0,
        "no_maa": 0,
    }
    category_usage = {cid: 0 for cid in MEETING_PLAYBOOK_CATEGORIES}
    category_missed = {cid: 0 for cid in MEETING_PLAYBOOK_CATEGORIES}

    for sid in sessions:
        counts["total_dirs"] += 1
        sdir = RESULTS_PATH / sid
        old = _load(sdir / "quality.json")
        new = _load(sdir / "quality_v2.json")
        new_plan = _load(sdir / "next_call_plan_v2.json")

        if old and old.get("skip_reason") == "too_short":
            counts["too_short"] += 1
            continue

        if not new:
            counts["missing_v2"] += 1
            continue
        counts["has_v2"] += 1

        maa = new.get("meeting_argumentation_assessment") or {}
        push = maa.get("overall_push_quality")
        if push == "weak":
            counts["weak_push"] += 1
        elif push == "adequate":
            counts["adequate_push"] += 1
        elif push == "strong":
            counts["strong_push"] += 1
        elif push == "not_applicable":
            counts["not_applicable_push"] += 1
        else:
            counts["no_maa"] += 1

        for a in maa.get("arguments_used") or []:
            cid = a.get("category_id")
            if cid in category_usage:
                category_usage[cid] += 1
        for m in maa.get("missed_opportunities") or []:
            cid = m.get("recommended_category_id")
            if cid in category_missed:
                category_missed[cid] += 1

        interest, reasons = _score_interestingness(old or {}, new, new_plan)
        rows.append({
            "session_id": sid,
            "interest": interest,
            "reasons": reasons,
            "old_score": (old or {}).get("overall_score"),
            "new_score": new.get("overall_score"),
            "push_quality": push,
            "missed_count": len(maa.get("missed_opportunities") or []),
            "args_used_count": len(maa.get("arguments_used") or []),
            "classification": (new.get("call_classification") or {}).get("type"),
            "outcome": (new.get("conversation_outcome") or {}).get("result"),
            "brief_new": (new.get("brief_summary") or "")[:200],
            "missed": maa.get("missed_opportunities") or [],
        })

    rows.sort(key=lambda r: (-r["interest"], r["session_id"]))

    return {
        "counts": counts,
        "category_usage": category_usage,
        "category_missed": category_missed,
        "rows": rows,
    }


def _fmt_counts(counts: dict) -> str:
    lines = []
    lines.append(f"Total session dirs: {counts['total_dirs']}")
    lines.append(f"  with quality_v2:  {counts['has_v2']}")
    lines.append(f"  missing v2:       {counts['missing_v2']}")
    lines.append(f"  too_short:        {counts['too_short']}")
    lines.append("")
    lines.append("Meeting push quality breakdown (of has_v2):")
    lines.append(f"  weak:            {counts['weak_push']}")
    lines.append(f"  adequate:        {counts['adequate_push']}")
    lines.append(f"  strong:          {counts['strong_push']}")
    lines.append(f"  not_applicable:  {counts['not_applicable_push']}")
    lines.append(f"  missing MAA:     {counts['no_maa']}")
    return "\n".join(lines)


def _fmt_category_usage(usage: dict, missed: dict, top: int = 10) -> str:
    combined = [(cid, usage.get(cid, 0), missed.get(cid, 0)) for cid in MEETING_PLAYBOOK_CATEGORIES]
    combined.sort(key=lambda x: -(x[1] + x[2]))
    lines = [f"{'category_id':<32} {'used':>6} {'missed':>7}"]
    lines.append("-" * 50)
    for cid, used, miss in combined[:top]:
        if used == 0 and miss == 0:
            continue
        lines.append(f"{cid:<32} {used:>6} {miss:>7}")
    return "\n".join(lines)


def _fmt_top(rows: list[dict], top: int = 20) -> str:
    out = []
    for i, r in enumerate(rows[:top], start=1):
        old_s = r["old_score"] if r["old_score"] is not None else "—"
        new_s = r["new_score"] if r["new_score"] is not None else "—"
        out.append(
            f"\n[{i}] {r['session_id']}  interest={r['interest']}\n"
            f"     score {old_s} → {new_s} | push={r['push_quality']} "
            f"| missed={r['missed_count']} used={r['args_used_count']} "
            f"| {r['classification']} / {r['outcome']}"
        )
        if r["reasons"]:
            out.append("     reasons: " + "; ".join(r["reasons"]))
        if r["brief_new"]:
            out.append(f"     summary: {r['brief_new']}")
        for m in (r.get("missed") or [])[:3]:
            tq = (m.get("trigger_quote") or "").strip().replace("\n", " ")[:120]
            cat = m.get("recommended_category_id") or ""
            out.append(f"       · клиент: «{tq}» → `{cat}`")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=20, help="Top N interesting calls to show")
    parser.add_argument("--json-out", type=str, default=None, help="Also dump full analysis to JSON file")
    args = parser.parse_args()

    data = analyze()
    print(_fmt_counts(data["counts"]))
    print()
    print("Playbook category usage (top 10):")
    print(_fmt_category_usage(data["category_usage"], data["category_missed"]))
    print()
    print(f"Top {args.top} interesting calls (sorted by interestingness score):")
    print(_fmt_top(data["rows"], args.top))

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nFull analysis saved to {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
