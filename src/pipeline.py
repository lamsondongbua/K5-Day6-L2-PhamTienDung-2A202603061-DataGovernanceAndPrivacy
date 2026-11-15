"""T06 PoC — Dataset Semantic Integrity & Provenance Gate.

Gate PASS / HOLD / REWORK for 20 candidate dataset releases (child of parent v1.0).
Silent defects = JSON stays valid, meaning is wrong. Invariants checked (all computed from
the INPUT only — never fixtures/*/gt/):

  id_collision    duplicate annotation ids / image ids
  class_swap      category id->name differs from the declared label map (same names, ids swapped)
                  or matched boxes carry a different class name than in the parent
  stale_map       category set differs from the declared label map version (e.g. `truck` missing),
                  annotations use unknown category ids, unknown label map, or parent sha256 mismatch
  bbox_shift      boxes outside the image / non-positive size, or too many boxes that no longer
                  overlap the parent's box on the same frame (IoU < iou_match)
  dropped_frames  parent frames missing from the child, or gaps in frame_index inside a sequence

"Benign" changes (a few jittered boxes, one added frame) must NOT block: thresholds below were
chosen from that margin and are FROZEN (override only through config params, before evaluation).

Run:  python -m src.main --config configs/lab.yaml --track t06 --output out/poc --run-name poc --offline --seed 42
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

from .common.context import RunContext
from .common.contact_sheet import render_contact_sheet
from .common.decision import Decision, canonical_status, write_decision
from .common.manifest import verify_manifest
from .common.review import (apply_overrides, load_review_decisions, make_review_queue,
                            write_review_queue)
from .metrics.tracks import get_track

# ---- frozen policy (see module docstring) ----------------------------------------------
DEFAULTS = {
    "parent_release": "releases/v1.0",
    "iou_match": 0.5,          # a child box "matches" the parent box on the same frame if IoU >= this
    "shift_frac": 0.05,        # > 5% of boxes unmatched -> bbox_shift (benign jitter touches < 1%)
    "swap_frac": 0.05,         # > 5% of matched boxes changed class name -> class_swap
    "dropped_min": 3,          # >= 3 parent frames missing (benign diffs touch <= 1 frame)
    "budget": 10,              # review budget B
}
# cause -> (weight in score, status when it is the primary root cause)
CAUSES = {
    "id_collision": (1.0, "HOLD"),
    "class_swap": (1.0, "HOLD"),
    "stale_map": (1.0, "HOLD"),
    "bbox_shift": (0.8, "REWORK"),
    "dropped_frames": (0.8, "REWORK"),
}
PRIORITY = ["id_collision", "class_swap", "stale_map", "bbox_shift", "dropped_frames"]


# =========================================================== pure helpers (unit-tested)
def _iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    iw = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    ih = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _names(ann: dict) -> dict:
    return {c["id"]: c["name"] for c in ann.get("categories", [])}


def analyze_release(ann: dict, meta: dict, parent: dict, label_maps: dict, params: dict,
                    parent_sha: str | None = None) -> dict:
    """Return {flags: {cause: bool}, metrics: {...}, evidence: {...}} for one candidate release."""
    p = {**DEFAULTS, **(params or {})}
    flags = {k: False for k in CAUSES}
    why: dict[str, list[str]] = defaultdict(list)
    ev: dict = {"file": None, "boxes": []}

    images, anns = ann.get("images", []), ann.get("annotations", [])
    cname = _names(ann)
    img_by_id = {i["id"]: i for i in images}

    # -- id collision ------------------------------------------------------------------
    dup_ann = sum(c - 1 for c in Counter(a["id"] for a in anns).values() if c > 1)
    dup_img = sum(c - 1 for c in Counter(i["id"] for i in images).values() if c > 1)
    # NB: duplicate image *file_name* is NOT a signal: benign releases re-use a file for their added frame.
    dup_file = sum(c - 1 for c in Counter(i["file_name"] for i in images).values() if c > 1)
    if dup_ann or dup_img:
        flags["id_collision"] = True
        why["id_collision"].append(f"duplicate ids: annotations={dup_ann}, images={dup_img}")
        seen, bad = set(), None
        for a in anns:
            if a["id"] in seen:
                bad = a
                break
            seen.add(a["id"])
        if bad and bad["image_id"] in img_by_id:
            ev = {"file": img_by_id[bad["image_id"]]["file_name"],
                  "boxes": [dict(zip("xywh", bad["bbox"]), label=f"dup ann id {bad['id']}", color="red")]}

    # -- label map / class semantics ---------------------------------------------------
    declared = label_maps.get(meta.get("label_map_version"))
    used_ids = {a["category_id"] for a in anns}
    if declared is None:
        flags["stale_map"] = True
        why["stale_map"].append(f"unknown label_map_version {meta.get('label_map_version')!r}")
    else:
        exp = {int(k): v for k, v in declared.items()}
        if set(cname.values()) != set(exp.values()) or not used_ids <= set(cname):
            flags["stale_map"] = True
            miss = sorted(set(exp.values()) - set(cname.values()))
            unk = sorted(used_ids - set(cname))
            why["stale_map"].append(f"categories differ from {meta['label_map_version']}: missing={miss}, unknown ids used={unk}")
        elif cname != exp:
            flags["class_swap"] = True
            diff = {i: f"{cname[i]} (expected {exp[i]})" for i in exp if cname.get(i) != exp[i]}
            why["class_swap"].append(f"id->name differs from {meta['label_map_version']}: {diff}")
    if parent_sha is not None and meta.get("parent_sha256") != parent_sha:
        flags["stale_map"] = True
        why["stale_map"].append("parent_sha256 does not match parent annotations.json")

    # -- geometry vs parent -------------------------------------------------------------
    pnames = _names(parent)
    pimg = {i["id"]: i["file_name"] for i in parent.get("images", [])}
    pboxes = defaultdict(list)
    for a in parent.get("annotations", []):
        pboxes[pimg.get(a["image_id"])].append((pnames.get(a["category_id"]), tuple(a["bbox"])))

    oob = 0
    n_cmp = unmatched = matched = renamed = 0
    worst_file, worst_n = None, 0
    unmatched_by_file, renamed_by_file = Counter(), Counter()
    unm_boxes: dict[str, list] = defaultdict(list)
    for a in anns:
        im = img_by_id.get(a["image_id"])
        if im is None:
            continue
        x, y, w, h = a["bbox"]
        if w <= 0 or h <= 0 or x < -0.5 or y < -0.5 or x + w > im["width"] + 0.5 or y + h > im["height"] + 0.5:
            oob += 1
        cands = pboxes.get(im["file_name"])
        if not cands:
            continue                      # frame not in parent (added frame) -> nothing to compare
        n_cmp += 1
        best = max(cands, key=lambda q: _iou(q[1], a["bbox"]))
        if _iou(best[1], a["bbox"]) >= p["iou_match"]:
            matched += 1
            if a["category_id"] in cname and cname[a["category_id"]] != best[0]:   # unknown ids belong to stale_map
                renamed += 1
                renamed_by_file[im["file_name"]] += 1
        else:
            unmatched += 1
            unmatched_by_file[im["file_name"]] += 1
            unm_boxes[im["file_name"]].append((a["bbox"], best[1]))
    shift_frac = unmatched / n_cmp if n_cmp else 0.0
    swap_frac = renamed / matched if matched else 0.0
    if oob or shift_frac > p["shift_frac"]:
        flags["bbox_shift"] = True
        why["bbox_shift"].append(f"{unmatched}/{n_cmp} boxes no longer overlap parent (IoU<{p['iou_match']}), {oob} out of image")
        if unmatched_by_file:
            f, _ = unmatched_by_file.most_common(1)[0]
            ev = {"file": f, "boxes": [d for new, old in unm_boxes[f]
                                       for d in (dict(zip("xywh", old), label="parent", color="lime"),
                                                 dict(zip("xywh", new), label="child", color="red"))]}
    if swap_frac > p["swap_frac"]:
        flags["class_swap"] = True
        why["class_swap"].append(f"{renamed}/{matched} matched boxes changed class name vs parent")
        if not ev["file"] and renamed_by_file:
            ev = {"file": renamed_by_file.most_common(1)[0][0], "boxes": []}

    # -- dropped frames ------------------------------------------------------------------
    cfiles = {i["file_name"] for i in images}
    missing = sorted(set(pimg.values()) - cfiles)
    by_seq = defaultdict(list)
    for i in images:
        by_seq[i.get("sequence_id")].append(i.get("frame_index", 0))
    gaps = sum(1 for l in by_seq.values() for u, v in zip(sorted(l), sorted(l)[1:]) if v - u != 1)
    if len(missing) >= p["dropped_min"] or gaps:
        flags["dropped_frames"] = True
        why["dropped_frames"].append(f"{len(missing)} parent frames missing, {gaps} frame_index gaps")
        if missing and not ev["file"]:
            ev = {"file": missing[0], "boxes": []}

    if not ev["file"] and images:
        ev = {"file": images[0]["file_name"], "boxes": []}
    return {
        "flags": flags,
        "why": {k: "; ".join(v) for k, v in why.items()},
        "metrics": {"dup_ann": dup_ann, "dup_img": dup_img, "dup_file": dup_file, "shift_frac": round(shift_frac, 4),
                    "swap_frac": round(swap_frac, 4), "oob": oob, "missing_frames": len(missing), "frame_gaps": gaps,
                    "n_images": len(images), "n_annotations": len(anns)},
        "evidence": ev,
    }


def decide_release(res: dict) -> tuple[str, str, float]:
    """(status, root_cause, score). Status PASS when nothing is flagged."""
    on = [c for c in PRIORITY if res["flags"][c]]
    m = res["metrics"]
    # continuous part keeps unflagged-but-drifting releases ahead of pristine ones in the review queue
    score = sum(CAUSES[c][0] for c in on) + m["shift_frac"] + m["swap_frac"] + 0.01 * m["missing_frames"]
    if not on:
        return "PASS", "clean", score
    return CAUSES[on[0]][1], on[0], score


# ---------------------------------------------------------------- 01 load
def load(ctx: RunContext) -> dict:
    if ctx.track != "t06":                       # this PoC implements T06 only -> explicit exit code 3
        raise NotImplementedError(f"src/pipeline.py implements T06 only (got {ctx.track})")
    manifest = ctx.input_dir / "manifest.csv"
    problems = verify_manifest(manifest) if manifest.exists() else [{"problem": "no_manifest"}]
    if problems:
        ctx.audit.log(actor="pipeline", action="provenance_problems", item_id=str(manifest), after=problems[:20])
        if ctx.config.get("strict_provenance", True):
            raise ValueError(f"provenance check failed ({len(problems)} problems), first: {problems[:3]}")
    parent_dir = ctx.input_dir / ctx.param("parent_release", DEFAULTS["parent_release"])
    parent_path = parent_dir / "annotations.json"
    if not parent_path.exists():
        raise FileNotFoundError(f"parent release not found: {parent_path}")
    return {
        "releases": pd.read_csv(ctx.input_dir / "releases.csv"),
        "parent": json.loads(parent_path.read_text(encoding="utf-8")),
        "parent_sha": _sha256(parent_path),
        "label_maps": json.loads((ctx.input_dir / "releases/label_maps.json").read_text(encoding="utf-8")),
    }


# ---------------------------------------------------------------- 02 process
def process(ctx: RunContext, data: dict) -> pd.DataFrame:
    params = {k: ctx.param(k, v) for k, v in DEFAULTS.items()}
    rows, boxes = [], {}
    for r in data["releases"].itertuples(index=False):
        rel = ctx.input_dir / r.path
        try:
            ann = json.loads((rel / "annotations.json").read_text(encoding="utf-8"))
            meta = json.loads((rel / "release_meta.json").read_text(encoding="utf-8"))
            res = analyze_release(ann, meta, data["parent"], data["label_maps"], params, data["parent_sha"])
            status, cause, score = decide_release(res)
            reason = " | ".join(res["why"].get(c, "") for c in PRIORITY if res["flags"][c]) or "all invariants hold"
            m, ev = res["metrics"], res["evidence"]
        except (OSError, ValueError, KeyError) as e:      # unreadable / malformed release must block, never pass
            status, cause, score, reason, m = "HOLD", "stale_map", 5.0, f"unreadable release: {type(e).__name__}: {e}", {}
            ev = {"file": None, "boxes": []}
        boxes[r.release_id] = ev["boxes"]
        rows.append({"release_id": r.release_id, "decision": status, "root_cause": cause, "score": round(score, 6),
                     "reason": reason, "evidence_path": ev["file"] or "", "split": r.split, **m})
    df = pd.DataFrame(rows)
    df.attrs["boxes"] = boxes
    return df


# ---------------------------------------------------------------- 03 rank
def rank(ctx: RunContext, scored: pd.DataFrame) -> pd.DataFrame:
    """Top-B releases by score. Contact sheet shows parent boxes (green) vs child boxes (red)."""
    budget = int(ctx.param("budget", DEFAULTS["budget"]))
    q = make_review_queue(scored, id_col="release_id", score_col="score", budget=budget, reason_col="reason",
                          action_col="decision", slice_col="root_cause", evidence_col="evidence_path")
    write_review_queue(q, ctx.out("review_queue.csv"))
    q = q[q["evidence_path"] != ""]
    render_contact_sheet(q, ctx.out("contact_sheet.html"), image_root=ctx.input_dir,
                         title="T06 release gate — review queue (green = parent box, red = child box)",
                         boxes_by_item=scored.attrs.get("boxes"))
    return q


# ---------------------------------------------------------------- 04 review
def review(ctx: RunContext, scored: pd.DataFrame, queue: pd.DataFrame) -> pd.DataFrame:
    """Apply reviewer decisions from out/review_queue_reviewed.csv (all changes go to the audit log).

    approve / skip : keep the automatic decision
    escalate       : force HOLD
    edit / reject  : need override_value in {PASS, HOLD, REWORK}; it replaces the decision
    """
    out = scored.copy()
    reviewed_path = ctx.output_dir / "review_queue_reviewed.csv"
    out["reviewed_by"] = ""
    if not reviewed_path.exists():
        return out
    reviewed = load_review_decisions(reviewed_path)
    ctx.stats["n_reviewed"] = len(reviewed)
    for r in reviewed.itertuples(index=False):
        if r.reviewer_decision in {"edit", "reject"}:
            if str(r.override_value).strip() == "":
                raise ValueError(f"{r.item_id}: `{r.reviewer_decision}` needs override_value in PASS/HOLD/REWORK")
            canonical_status(str(r.override_value))          # validates
    reviewed = reviewed.assign(override_value=reviewed["override_value"].str.strip().str.upper())
    esc = reviewed[reviewed.reviewer_decision == "escalate"]
    for r in esc.itertuples(index=False):
        before = out.loc[out.release_id == r.item_id, "decision"].tolist()
        out.loc[out.release_id == r.item_id, "decision"] = "HOLD"
        ctx.audit.log(actor=r.reviewer, action="override:escalate", item_id=r.item_id,
                      before=before[0] if before else None, after="HOLD", reason=r.review_note)
    out = apply_overrides(out, reviewed, id_col="release_id", value_col="decision", audit=ctx.audit)
    for r in reviewed.itertuples(index=False):
        out.loc[out.release_id == r.item_id, "reviewed_by"] = r.reviewer
        if r.reviewer_decision in {"approve", "skip"}:
            ctx.audit.log(actor=r.reviewer, action=f"review:{r.reviewer_decision}", item_id=r.item_id,
                          reason=r.review_note)
    return out


# ---------------------------------------------------------------- 05 decide
def decide(ctx: RunContext, final: pd.DataFrame) -> Decision:
    blocked = final[final.decision.map(lambda s: canonical_status(s) != "GO")]
    # rework report: file / class / bbox evidence per blocked release, with sign-off owner
    owner = ctx.config.get("owner", "TODO-owner")
    lines = ["# T06 rework report", "", f"Owner (sign-off): {owner}", "",
             "| release | decision | root cause | evidence frame | detail |", "|---|---|---|---|---|"]
    for r in blocked.sort_values("score", ascending=False).itertuples(index=False):
        lines.append(f"| {r.release_id} | {r.decision} | {r.root_cause} | {r.evidence_path} | {r.reason} |")
    ctx.out("rework_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    n, nb = len(final), len(blocked)
    metrics = {"n_releases": n, "n_blocked": nb, "n_pass": n - nb, "by_cause": blocked.root_cause.value_counts().to_dict()}
    if nb:
        return Decision(status="HOLD", reason_code="SILENT_DEFECTS_BLOCKED", owner=owner, scope=ctx.track,
                        reason=f"{nb}/{n} candidate releases blocked by semantic invariants; {n - nb} may ship",
                        evidence=[str(ctx.out("rework_report.md")), str(ctx.out("release_decisions.csv"))], metrics=metrics)
    return Decision(status="GO", reason_code="ALL_INVARIANTS_HOLD", owner=owner, scope=ctx.track,
                    reason=f"All {n} candidate releases passed every invariant",
                    evidence=[str(ctx.out("release_decisions.csv"))], metrics=metrics)


def run(ctx: RunContext) -> None:
    spec = get_track(ctx.track)
    data = load(ctx)
    scored = process(ctx, data)
    ctx.stats["n_items"] = len(scored)
    queue = rank(ctx, scored)
    final = review(ctx, scored, queue)
    final[spec.pred_columns + [c for c in final.columns if c not in spec.pred_columns]].to_csv(ctx.out(spec.pred_file), index=False)
    write_decision(decide(ctx, final), ctx.out("decision.json"), ctx.out("decision_log.csv"))
