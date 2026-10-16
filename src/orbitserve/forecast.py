"""Observed-state analytical reference forecasts; never imports the runtime.

All time coordinates are seconds on the observation's clock, not wall-clock UTC.
The provider projects queues/energy forward under no unobserved arrivals.  Contact
windows are public input.  There is deliberately no scenario/script parameter.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import uuid

METHOD_VERSION = "analytical-queue-contact-v1"


def _number(value, name, minimum=0.0):
    x = float(value)
    if not math.isfinite(x) or x < minimum:
        raise ValueError(f"{name} must be finite and >= {minimum}")
    return x


def _node_work(candidate_workload, node_id):
    if candidate_workload is None:
        return 0.0
    if isinstance(candidate_workload, dict) and "node_id" in candidate_workload:
        rows = [candidate_workload]
    else:
        rows = candidate_workload.get("nodes", candidate_workload.get("tasks", candidate_workload)) if isinstance(candidate_workload, dict) else candidate_workload
    if isinstance(rows, dict):
        value = rows.get(node_id, 0)
        return float(value.get("compute_pixels", value.get("estimated_compute_pixels", 0))) if isinstance(value, dict) else float(value)
    return sum(float(x.get("compute_pixels", x.get("estimated_compute_pixels", 0))) for x in rows if x.get("node_id") == node_id)


def _contact_edges(contacts, start, end):
    edges = []
    for c in contacts:
        source = c.get("source", c.get("source_node", c.get("src", c.get("from"))))
        destination = c.get("destination", c.get("destination_node", c.get("dst", c.get("to"))))
        if not source or not destination:
            raise ValueError("Each known contact needs source and destination")
        rate = c.get("throughput_bytes_s", c.get("bandwidth_bytes_s", c.get("rate_bytes_s", c.get("transfer_rate_bytes_s"))))
        if rate is None:
            raise ValueError("Contact throughput_bytes_s is required")
        rate = _number(rate, "throughput_bytes_s")
        windows = c.get("windows", c.get("available_intervals"))
        if windows is None:
            windows = [[c.get("start_s", c.get("start", start)), c.get("end_s", c.get("end", end))]]
        intervals = []
        for window in windows:
            if isinstance(window, dict):
                a, b = window.get("start_s", window.get("start")), window.get("end_s", window.get("end"))
            else:
                a, b = window[:2]
            a, b = max(start, float(a)), min(end, float(b))
            if b > a and rate > 0:
                intervals.append({"start_s": a, "end_s": b})
        intervals.sort(key=lambda x: x["start_s"])
        # Merge overlaps so public contact duplication cannot create bandwidth.
        merged = []
        for w in intervals:
            if merged and w["start_s"] <= merged[-1]["end_s"]:
                merged[-1]["end_s"] = max(merged[-1]["end_s"], w["end_s"])
            else:
                merged.append(w.copy())
        edges.append({"source": str(source), "destination": str(destination),
                      "available_intervals": merged, "throughput_bytes_s": rate,
                      "latency_s": _number(c.get("latency_s", 0), "latency_s"),
                      "predicted_capacity_bytes": sum(w["end_s"] - w["start_s"] for w in merged) * rate,
                      "source_kind": c.get("source_kind", "synthetic_contact_trace"),
                      "provenance": "caller_supplied_known_contact_plan"})
    return edges


def forecast(observation_history, known_contact_plan, horizon_s, candidate_workload=None,
             source_kind="analytical_reference"):
    """Project only supplied, timestamped observations and public contacts.

    node_forecasts contains interval endpoints, not a renamed current snapshot.
    Explicit candidate work is charged at the start of the forecast.  Future
    arrivals and resource changes not in observations are not guessed.
    """
    if source_kind == "learned_wm":
        raise NotImplementedError("learned_wm unavailable: no trained weights or adapter is configured; no automatic fallback")
    if source_kind != "analytical_reference":
        raise ValueError("forecast() accepts analytical_reference only; use paired_test_forecasts() for explicitly labelled fixtures")
    if not observation_history:
        raise ValueError("At least one timestamped observation is required")
    horizon = _number(horizon_s, "horizon_s", 0.000001)
    history = sorted(deepcopy(observation_history), key=lambda x: float(x["timestamp"]))
    snapshot = history[-1]
    start = _number(snapshot["timestamp"], "observation timestamp")
    end = start + horizon
    if not isinstance(snapshot.get("nodes"), list) or not snapshot["nodes"]:
        raise ValueError("Latest observation must contain nodes")
    node_forecasts = []
    for node in snapshot["nodes"]:
        node_id = str(node["node_id"])
        rate = _number(node.get("compute_pixels_s", 0), "compute_pixels_s")
        queue = _number(node.get("queue_work_pixels", 0), "queue_work_pixels")
        added = _number(_node_work(candidate_workload, node_id), "candidate compute pixels")
        queue += added
        blocked_until = max(start + _number(node.get("available_after_s", 0), "available_after_s"),
                            float(node.get("busy_until_s", start)))
        energy = _number(node.get("energy_j", 0), "energy_j")
        per_pixel = _number(node.get("energy_per_pixel_j", 0.00001), "energy_per_pixel_j")
        memory = int(_number(node.get("memory_bytes", 0), "memory_bytes"))
        storage = int(_number(node.get("storage_bytes", 0), "storage_bytes"))
        drain_at = blocked_until + queue / rate if rate > 0 else end
        cuts = sorted(set([start, end, *[start + horizon * f for f in (0.25, 0.5, 0.75)],
                           *[t for t in (blocked_until, drain_at) if start < t < end]]))
        segments = []
        for a, b in zip(cuts, cuts[1:]):
            served_a = min(queue, max(0.0, a - blocked_until) * rate, energy / per_pixel if per_pixel else queue)
            served_b = min(queue, max(0.0, b - blocked_until) * rate, energy / per_pixel if per_pixel else queue)
            queue_a, queue_b = max(0.0, queue - served_a), max(0.0, queue - served_b)
            remaining_energy = max(0.0, energy - served_b * per_pixel)
            spare = max(0.0, b - max(a, drain_at)) * rate
            if per_pixel:
                spare = min(spare, remaining_energy / per_pixel)
            segments.append({"start_s": a, "end_s": b, "compute_pixels_s": rate,
                             "available_compute_pixels": spare,
                             "queue_work_pixels": queue_a, "queue_end_work_pixels": queue_b,
                             "queue_wait_s": max(0, blocked_until - a) + (queue_a / rate if rate else horizon),
                             "memory_bytes": memory, "storage_bytes": storage,
                             "energy_start_j": max(0.0, energy - served_a * per_pixel),
                             "energy_j": remaining_energy,
                             "estimated_fields": ["available_compute_pixels", "queue_work_pixels", "queue_end_work_pixels", "queue_wait_s", "energy_start_j", "energy_j"],
                             "static_fields": ["compute_pixels_s", "memory_bytes", "storage_bytes"]})
        node_forecasts.append({"node_id": node_id, "capabilities": deepcopy(node.get("capabilities", [])),
                               "segments": segments, "observed_at": float(node.get("timestamp", start)),
                               "energy_model": {"kind": "estimated", "joules_per_pixel": per_pixel},
                               "assumption": "no unobserved arrivals; fixed observed resource limits; work-conserving service"})
    return {"schema_version": "1.0", "forecast_id": "forecast-" + uuid.uuid4().hex,
            "issued_at": start, "observation_cutoff": start, "horizon_s": horizon,
            "valid_until": end, "time_unit": "seconds", "clock": snapshot.get("clock", "run_monotonic"),
            "source_kind": source_kind, "model_or_method_version": METHOD_VERSION,
            "conditioning": {"kind": "candidate_workload" if candidate_workload is not None else "current_observed_workload",
                             "candidate_workload": deepcopy(candidate_workload)},
            "observed_snapshot": snapshot, "node_forecasts": node_forecasts,
            "edge_forecasts": _contact_edges(known_contact_plan, start, end),
            "uncertainty": {"status": "not_estimated", "confidence": None,
                            "limitations": ["No inference of hidden future arrivals or failures", "Energy is a configured model, not measured hardware power"]},
            "observation_history_count": len(history),
            "observation_digest": hashlib.sha256(json.dumps(history, sort_keys=True).encode()).hexdigest(),
            "feedback_event_ids": list(snapshot.get("feedback_event_ids", [])),
            "expired": False}


def mark_expired(forecast_graph, now_s):
    """Preserve prediction identity when checking freshness; never redate it."""
    value = deepcopy(forecast_graph)
    value["evaluated_at"] = float(now_s)
    value["expired"] = float(now_s) > float(value["valid_until"])
    return value


def paired_test_forecasts(observation_history, known_contact_plan, horizon_s=120.0):
    """E2 future-only intervention: ample vs constrained future memory.

    Both copies retain byte-identical observed_snapshot, capabilities, task and
    grid.  The constrained 11 MiB future budget admits 128x128 tiles + merge,
    but not a 250x250 tile under the documented working-set model.  This is a
    test fixture, not a claim that an analytical predictor foresaw that change.
    """
    base = forecast(observation_history, known_contact_plan, horizon_s)
    pair = []
    observed_nodes = {n["node_id"]: n for n in base["observed_snapshot"]["nodes"]}
    for name, memory in (("ample", 64 * 1024**2), ("constrained", 11 * 1024**2)):
        graph = deepcopy(base)
        graph["forecast_id"] = "fixture-" + uuid.uuid4().hex
        graph["source_kind"] = "test_fixture"
        graph["model_or_method_version"] = "future-memory-intervention-v1"
        graph["fixture_name"] = name
        graph["fixture_description"] = "Future analysis-node memory budget only; observed snapshot unchanged"
        for node in graph["node_forecasts"]:
            observed_memory = int(observed_nodes[node["node_id"]].get("memory_bytes", 0))
            if observed_memory >= 16 * 1024**2 and ("water_tile" in node["capabilities"] or "water_analysis" in node["capabilities"]):
                for segment in node["segments"]:
                    segment["memory_bytes"] = min(observed_memory, memory)
                    if "memory_bytes" in segment["static_fields"]:
                        segment["static_fields"].remove("memory_bytes")
                    segment["estimated_fields"].append("memory_bytes")
        pair.append(graph)
    assert pair[0]["observed_snapshot"] == pair[1]["observed_snapshot"]
    return tuple(pair)


# Descriptive alias for callers constructing the E2 evidence pair.
make_test_fixture_pair = paired_test_forecasts
