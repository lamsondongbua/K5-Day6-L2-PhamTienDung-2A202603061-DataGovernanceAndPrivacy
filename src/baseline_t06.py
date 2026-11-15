"""T06 BASELINE (naive): parse JSON + schema + file-exists only. NO semantic invariants.

Protocol of docs/tracks/T06.md: the baseline only checks that the release "opens":
  - annotations.json / release_meta.json parse
  - required keys exist
  - every image file referenced by the annotations exists
It deliberately does NOT compare class maps, bbox ranges, ids, frame continuity or
parent distribution, so silent defects (valid JSON, wrong meaning) slip through.
This is the bar the PoC (src/pipeline.py) must beat.

    python -m src.main --track t06 --pipeline src.baseline_t06 --output out/baseline --run-name baseline --offline --seed 42
"""
from __future__ import annotations

import json

import pandas as pd

from .common.context import RunContext
from .common.decision import Decision, write_decision
from .metrics.tracks import get_track

REQUIRED_ANN_KEYS = ("images", "annotations", "categories")
REQUIRED_META_KEYS = ("version", "parent_version", "parent_sha256", "label_map_version")


def _check_release(ctx: RunContext, rel_dir) -> list[str]:
    """Return list of structural problems (empty = looks fine)."""
    problems: list[str] = []
    ann_p, meta_p = rel_dir / "annotations.json", rel_dir / "release_meta.json"
    try:
        ann = json.loads(ann_p.read_text(encoding="utf-8"))
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return [f"unreadable_json:{type(e).__name__}"]
    problems += [f"missing_key:{k}" for k in REQUIRED_ANN_KEYS if k not in ann]
    problems += [f"missing_meta:{k}" for k in REQUIRED_META_KEYS if k not in meta]
    for im in ann.get("images", []):
        if not (ctx.input_dir / im.get("file_name", "")).exists():
            problems.append(f"missing_image:{im.get('file_name')}")
    return problems


def run(ctx: RunContext) -> None:
    spec = get_track(ctx.track)
    releases = pd.read_csv(ctx.input_dir / "releases.csv")                      # 01 load
    rows = []
    for r in releases.itertuples():                                              # 02 process (naive)
        problems = _check_release(ctx, ctx.input_dir / r.path)
        rows.append({
            "release_id": r.release_id,
            "decision": "HOLD" if problems else "PASS",
            "root_cause": "schema_error" if problems else "clean",
            "detail": ";".join(problems[:5]),
        })
    out = pd.DataFrame(rows)
    ctx.stats["n_items"] = len(out)
    out.to_csv(ctx.out(spec.pred_file), index=False)                             # 03-04 no ranking / review in baseline
    n_hold = int((out.decision == "HOLD").sum())
    write_decision(                                                              # 05 decide
        Decision(status="HOLD", reason_code="BASELINE_ONLY",
                 reason=f"Naive schema-only baseline ({n_hold}/{len(out)} blocked); not a release gate",
                 owner=ctx.config.get("owner", "baseline"), scope=ctx.track,
                 evidence=[str(ctx.out(spec.pred_file))]),
        ctx.out("decision.json"), ctx.out("decision_log.csv"))
