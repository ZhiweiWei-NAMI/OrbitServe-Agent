"""Forecast-conditioned rule DAG planner, with no final node assignment.

Candidate scoring is a conservative serial end-to-end estimate, not a schedule.
It includes every tile's transfer, queue, compute, return and startup, plus the
merge; allowing actual concurrency can improve on this bound.  The executable
DAG contains capability constraints and submission ownership, never placement.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import uuid

from .policy import _edge_delay, _has_capability

PLANNER_VERSION = "forecast-working-set-rule-v1"
_DEFAULTS = {"candidate_tile_sizes": [0, 128, 64], "operator_base_memory_bytes": 8388608,
             "water_tile_memory_bytes_per_pixel": 104, "water_merge_memory_bytes_per_pixel": 24,
             "operator_startup_s": 0.25, "water_tile_energy_j_per_pixel": 0.00001,
             "intermediate_bytes_per_pixel": 24, "final_bytes_per_pixel": 48}


def _settings(task):
    settings = dict(_DEFAULTS)
    config = Path(__file__).resolve().parents[2] / "configs" / "planning.json"
    if config.is_file():
        settings.update(json.loads(config.read_text()))
    settings.update(task.get("planning_config", {}))
    return settings


def _skills(catalog):
    if isinstance(catalog, list):
        rows = catalog
    else:
        rows = catalog.get("skills", catalog.get("operators", catalog.get("tools", catalog)))
    if isinstance(rows, dict):
        return {k: v if isinstance(v, dict) else {} for k, v in rows.items()}
    return {r.get("tool_id", r.get("skill_id", r.get("id"))): r for r in rows}


def _assets(catalog):
    if isinstance(catalog, list):
        return catalog
    return catalog.get("assets", [])


def _resource_cost(tool, pixels, input_bytes, output_bytes, settings, total_pixels=None):
    merge = tool == "water_merge"
    return {"estimated_memory_bytes": int(settings["operator_base_memory_bytes"] + pixels * settings["water_merge_memory_bytes_per_pixel" if merge else "water_tile_memory_bytes_per_pixel"]),
            "estimated_compute_pixels": int(pixels),
            "estimated_input_bytes": int(input_bytes), "estimated_output_bytes": int(output_bytes),
            "estimated_storage_bytes": int(input_bytes + output_bytes),
            "estimated_energy_j": float(pixels * settings["water_tile_energy_j_per_pixel"]),
            "estimated_startup_s": float(settings["operator_startup_s"])}


def _forecast_cost(step, graph, source, allowed):
    """Compare hypothetical node costs; do not commit any node to a step."""
    start = float(graph["issued_at"])
    horizon_end = float(graph["valid_until"])
    costs = []
    for node in graph["node_forecasts"]:
        node_id = node["node_id"]
        if node_id not in allowed:
            continue
        rejected = []
        if not _has_capability(node.get("capabilities", []), step["required_capability"]):
            rejected.append("missing_capability")
        segment_options = []
        for seg in node.get("segments", []):
            reasons = list(rejected)
            rate = float(seg.get("compute_pixels_s", 0))
            if rate <= 0:
                reasons.append("no_compute_capacity")
            for field, need in (("memory_bytes", "estimated_memory_bytes"), ("storage_bytes", "estimated_storage_bytes"), ("energy_j", "estimated_energy_j")):
                if float(seg.get(field, 0)) < step[need]:
                    reasons.append("insufficient_" + field)
            evaluation_at = max(start, float(seg.get("start_s", start)))
            wait, transfer = _edge_delay(graph, source, node_id, step["estimated_input_bytes"], evaluation_at)
            if wait is None:
                reasons.append("no_input_contact_capacity")
            queue_s = float(seg.get("queue_wait_s", float(seg.get("queue_work_pixels", 0)) / rate if rate else graph["horizon_s"]))
            compute_s = step["estimated_compute_pixels"] / rate + step["estimated_startup_s"] if rate else 0
            ready_to_return = evaluation_at + (wait or 0) + (transfer or 0) + queue_s + compute_s
            rw, rt = _edge_delay(graph, node_id, source, step["estimated_output_bytes"], ready_to_return)
            if rw is None:
                reasons.append("no_return_contact_capacity")
            cost = evaluation_at - start + (wait or 0) + (transfer or 0) + queue_s + compute_s + (rw or 0) + (rt or 0)
            if start + cost > horizon_end:
                reasons.append("exceeds_prediction_horizon")
            segment_options.append({"evaluated_at": evaluation_at, "feasible": not reasons,
                                    "rejection_reasons": reasons, "total_s": cost if not reasons else None,
                                    "contact_wait_s": wait, "input_transfer_s": transfer,
                                    "queue_s": queue_s, "compute_s": compute_s,
                                    "return_s": None if rw is None else rw + rt})
        feasible = [x for x in segment_options if x["feasible"]]
        best = min(feasible, key=lambda x: x["total_s"]) if feasible else None
        costs.append({"node_id": node_id, "feasible": bool(best), "minimum_cost_s": best["total_s"] if best else None,
                      "best_cost": best, "segment_evaluations": segment_options})
    feasible_nodes = [c for c in costs if c["feasible"]]
    return {"feasible": bool(feasible_nodes), "minimum_cost_s": min(c["minimum_cost_s"] for c in feasible_nodes) if feasible_nodes else None,
            "hypothetical_node_costs": costs}


def plan(task, asset_catalog, skill_catalog, forecast_graph):
    """Select whole/128/64 grain using forecast resource and contact costs."""
    settings = _settings(task)
    mode = task.get("planner_mode", settings.get("planner_mode", "rule"))
    if mode != "rule":
        raise NotImplementedError(f"{mode} planner unavailable: no LLM planner adapter configured; no automatic fallback")
    if not forecast_graph.get("forecast_id") or not forecast_graph.get("node_forecasts"):
        raise ValueError("ForecastGraph identity and future node forecasts are required")
    now = float(task.get("planning_time_s", forecast_graph["issued_at"]))
    if forecast_graph.get("expired") or now > float(forecast_graph["valid_until"]):
        raise ValueError("Forecast is expired; obtain a new forecast instead of redating it")
    width, height = int(task["width"]), int(task["height"])
    if width <= 0 or height <= 0:
        raise ValueError("Task grid dimensions must be positive")
    total_pixels = width * height
    source = str(task.get("source_node", "A"))
    observed = forecast_graph["observed_snapshot"]
    allowed = list(task.get("allowed_nodes") or [n["node_id"] for n in observed["nodes"]])
    skills = _skills(skill_catalog)
    for tool in ("water_tile", "water_merge"):
        if skills and tool not in skills:
            raise ValueError(f"Skill catalog does not provide required operator {tool}")
        capable = [n["node_id"] for n in observed["nodes"] if n["node_id"] in allowed and _has_capability(n.get("capabilities", []), skills.get(tool, {}).get("required_capability", tool))]
        if len(capable) < 2:
            raise ValueError(f"{tool} needs at least two genuinely capable allowed execution nodes; found {capable}")
    assets = _assets(asset_catalog)
    # Actual file sizes, when provided, bound transfer/storage estimates. These
    # are metadata only; the planner never opens raw imagery or worker storage.
    full_input = sum(int(a.get("size_bytes", a.get("bytes", a.get("file_size_bytes", 0)))) for a in assets)
    if not full_input:
        full_input = total_pixels * 12 * 2  # six uint16 bands at two dates
    asset_refs = [a.get("artifact_id", a.get("asset_id", a.get("path"))) for a in assets]
    asset_refs = [r for r in asset_refs if r is not None]
    threshold = float(task.get("threshold", 0.0))
    if not math.isfinite(threshold):
        raise ValueError("Scientific threshold must be finite and shared across all tiles")
    method = task.get("method", "MNDWI")
    if method != "MNDWI":
        raise ValueError("This rule planner supports the registered MNDWI water workflow only")
    candidates = []
    tile_sizes = []
    for requested in settings["candidate_tile_sizes"]:
        size = max(width, height) if int(requested) == 0 else int(requested)
        if size > 0 and size not in tile_sizes:
            tile_sizes.append(size)
    for size in tile_sizes:
        windows = [[r, c, min(size, height - r), min(size, width - c)]
                   for r in range(0, height, size) for c in range(0, width, size)]
        steps, evaluations = [], []
        for idx, window in enumerate(windows):
            pixels = window[2] * window[3]
            # The current runtime ships a complete case package on first use.
            # Charge complete inputs for each step: conservative, no invented
            # network savings from windowing or an unverified cache hit.
            resources = _resource_cost("water_tile", pixels, full_input, pixels * settings["intermediate_bytes_per_pixel"], settings)
            step = {"step_id": f"water_tile_{idx:03d}", "tool_id": "water_tile", "dependencies": [],
                    "params": {"window": window, "threshold": threshold, "method": method},
                    "required_capability": skills.get("water_tile", {}).get("required_capability", "water_tile"),
                    "allowed_nodes": list(allowed), "owner_node": source, "submission_owner": source,
                    "result_owner": source, "input_refs": deepcopy(asset_refs), **resources}
            steps.append(step)
            evaluations.append(_forecast_cost(step, forecast_graph, source, allowed))
        merge = {"step_id": "water_merge", "tool_id": "water_merge", "dependencies": [s["step_id"] for s in steps],
                 "params": {"threshold": threshold, "method": method, "tile_step_ids": [s["step_id"] for s in steps]},
                 "required_capability": skills.get("water_merge", {}).get("required_capability", "water_merge"),
                 "allowed_nodes": list(allowed), "owner_node": source, "submission_owner": source, "result_owner": source,
                 "input_refs": ["step:" + s["step_id"] for s in steps],
                 **_resource_cost("water_merge", total_pixels, total_pixels * settings["intermediate_bytes_per_pixel"],
                                  total_pixels * settings["final_bytes_per_pixel"], settings)}
        steps.append(merge)
        evaluations.append(_forecast_cost(merge, forecast_graph, source, allowed))
        feasible = all(e["feasible"] for e in evaluations)
        score = sum(e["minimum_cost_s"] for e in evaluations) if feasible else None
        if score is not None and now + score > float(forecast_graph["valid_until"]):
            feasible = False
        candidates.append({"tile_size": size, "grain": "whole" if len(windows) == 1 else "tiled",
                           "tile_count": len(windows), "windows": windows,
                           "peak_memory_bytes": max(s["estimated_memory_bytes"] for s in steps),
                           "estimated_intermediate_bytes": total_pixels * settings["intermediate_bytes_per_pixel"],
                           "estimated_input_transfer_bytes_upper_bound": len(windows) * full_input,
                           "estimated_completion_s": score, "feasible": feasible,
                           "cost_model": "sum_of_minimum_per_step_end_to_end_costs_conservative_serial_no_cache_credit",
                           "step_cost_evaluations": evaluations, "steps": steps})
    viable = [c for c in candidates if c["feasible"]]
    if not viable:
        error = ValueError("No candidate grain fits forecast resources, contact capacity and horizon")
        error.candidate_evaluations = [{k: v for k, v in c.items() if k != "steps"} for c in candidates]
        raise error
    chosen = min(viable, key=lambda c: (c["estimated_completion_s"], c["tile_count"]))
    mission_id = str(task.get("mission_id", "mission-" + str(task.get("case_id", "water"))))
    plan_id = "plan-" + uuid.uuid4().hex
    for step in chosen["steps"]:
        step.update({"plan_id": plan_id, "plan_version": 1, "mission_id": mission_id})
    return {"schema_version": "1.0", "plan_id": plan_id, "version": 1,
            "mission_id": mission_id, "case_id": task.get("case_id"), "business_goal": "Compare water coverage across the original AOI and dates",
            "forecast_id": forecast_graph["forecast_id"], "forecast_source_kind": forecast_graph["source_kind"],
            "planner_mode": "rule", "planner_version": PLANNER_VERSION, "created_at": now,
            "grain": chosen["grain"], "tile_size": chosen["tile_size"], "tile_windows": chosen["windows"],
            "source_node": source, "submission_owner": source, "steps": chosen["steps"],
            "scientific_constraints": {"method": method, "threshold": threshold, "width": width, "height": height,
                                       "crs": task.get("crs"), "transform": deepcopy(task.get("transform")),
                                       "pixel_size_m": task.get("pixel_size_m", 20),
                                       "validity_rule": "V_before intersection V_after intersection original_AOI",
                                       "unknown_class": 0, "classification": {"1": "stable_nonwater", "2": "stable_water", "3": "gained", "4": "lost"},
                                       "coverage_rule": "every original grid cell exactly once; no resampling or AOI reduction for scheduling",
                                       "tile_boundary_rule": "half_open_row_column_windows_no_overlap_no_halo_required"},
            "delivery": {"destination_node": task.get("ground_node", "G"), "formats": ["GeoTIFF", "GeoJSON", "PNG", "JSON"],
                         "required_files": ["change.tif", "before_water.tif", "after_water.tif", "valid_common.tif", "before_rgb.tif", "after_rgb.tif", "stats.json", "changes.geojson", "map.png"],
                         "completion_condition": "ground_received_all_required_files_and_verified_content_hashes"},
            "selection": {"reason": "minimum_feasible_forecast_cost", "estimated_completion_s": chosen["estimated_completion_s"],
                          "candidate_evaluations": [{k: v for k, v in c.items() if k != "steps"} for c in candidates]},
            "ownership_semantics": "A/source owns submission and dependency discovery only; each worker policy and target admission decide execution"}
