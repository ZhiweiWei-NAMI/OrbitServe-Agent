"""Node-local rule policy; the broker must never call this for a worker.

Only local measurements, timestamped received advertisements and a previously
received forecast are consumed.  This module performs no file or network I/O.
A request is only a request: the destination independently performs admission.
"""
from __future__ import annotations

import math

POLICY_VERSION = "local-advertisement-cost-v1"


def _has_capability(capabilities, required):
    if isinstance(capabilities, dict):
        capabilities = [k for k, v in capabilities.items() if v]
    return required in capabilities or (required in ("water_tile", "water_merge") and "water_analysis" in capabilities)


def _edge_delay(forecast_view, source, destination, byte_count, now):
    if source == destination:
        return 0.0, 0.0
    matches = [e for e in forecast_view.get("edge_forecasts", [])
               if e.get("source", e.get("src")) == source and e.get("destination", e.get("dst")) == destination]
    best = None
    for edge in matches:
        rate = float(edge.get("throughput_bytes_s", edge.get("bandwidth_bytes_s", 0)))
        if rate <= 0:
            continue
        left, cursor, first = max(0, byte_count), now, None
        for window in edge.get("available_intervals", edge.get("windows", [])):
            a, b = (window["start_s"], window["end_s"]) if isinstance(window, dict) else window[:2]
            begin = max(cursor, float(a))
            if begin >= float(b):
                continue
            if first is None:
                first = begin
            duration = min(left / rate, float(b) - begin)
            left -= duration * rate
            cursor = begin + duration
            if left <= 1e-6:
                candidate = (first - now, cursor - first + float(edge.get("latency_s", 0)))
                if best is None or sum(candidate) < sum(best):
                    best = candidate
                break
    return best if best is not None else (None, None)


def choose_action(local_observation, task, received_neighbors, forecast_view):
    """Return local/request/wait plus all candidate costs and rejections."""
    mode = local_observation.get("policy_mode", task.get("policy_mode", "rule"))
    if mode != "rule":
        raise NotImplementedError(f"{mode} execution policy unavailable; MARL weights/adapter are not configured")
    actor = str(local_observation["node_id"])
    now = float(local_observation.get("now", local_observation.get("timestamp", 0)))
    required = task.get("required_capability", task.get("tool_id"))
    if not required:
        raise ValueError("Task needs required_capability or tool_id")
    allowed = set(task.get("allowed_nodes") or [])
    need_memory = float(task.get("estimated_memory_bytes", 0))
    need_storage = float(task.get("estimated_storage_bytes", task.get("estimated_output_bytes", task.get("output_bytes", 0))))
    need_energy = float(task.get("estimated_energy_j", 0))
    pixels = float(task.get("estimated_compute_pixels", task.get("compute_pixels", 0)))
    input_bytes = float(task.get("input_bytes", task.get("estimated_input_bytes", 0)))
    output_bytes = float(task.get("output_bytes", task.get("estimated_output_bytes", 0)))
    return_node = str(task.get("result_owner", task.get("owner_node", task.get("source_node", actor))))
    max_age = float(local_observation.get("advertisement_max_age_s", 30))
    records = [(local_observation, True)]
    # Newest received ad wins; no use of forecast node queues/capabilities here.
    latest = {}
    for ad in received_neighbors:
        node_id = str(ad.get("node_id", ""))
        if node_id and node_id != actor and (node_id not in latest or float(ad.get("timestamp", -1)) > float(latest[node_id].get("timestamp", -1))):
            latest[node_id] = ad
    records.extend((latest[k], False) for k in sorted(latest))
    candidates = []
    for node, is_local in records:
        node_id = str(node["node_id"])
        reasons = []
        timestamp = node.get("timestamp", local_observation.get("timestamp", now) if is_local else None)
        age = now - float(timestamp) if timestamp is not None else None
        if not is_local and timestamp is None:
            reasons.append("missing_advertisement_timestamp")
        elif not is_local and (age > max_age or age < -0.5):
            reasons.append("stale_or_future_advertisement")
        if allowed and node_id not in allowed:
            reasons.append("node_not_allowed")
        if not _has_capability(node.get("capabilities", []), required):
            reasons.append("missing_capability")
        rate = float(node.get("compute_pixels_s", 0))
        if rate <= 0:
            reasons.append("no_compute_capacity")
        if float(node.get("memory_bytes", 0)) < need_memory:
            reasons.append("insufficient_memory")
        if float(node.get("storage_bytes", 0)) < need_storage:
            reasons.append("insufficient_storage")
        if float(node.get("energy_j", 0)) < need_energy:
            reasons.append("insufficient_energy")
        if node.get("service_available") is False:
            reasons.append("service_unavailable")
        node_input_bytes = float(node.get("input_bytes", input_bytes)) if not is_local else 0.0
        wait, transfer, return_time = 0.0, 0.0, 0.0
        if not is_local:
            if "transfer_rate_bytes_s" in node:
                throughput = float(node["transfer_rate_bytes_s"])
                wait = float(node.get("contact_wait_s", 0))
                if throughput <= 0 or not math.isfinite(wait):
                    reasons.append("no_known_input_contact")
                else:
                    transfer = node_input_bytes / throughput + float(node.get("latency_s", 0))
            else:
                wait, transfer = _edge_delay(forecast_view, actor, node_id, node_input_bytes, now)
                if wait is None:
                    reasons.append("no_known_input_contact")
        queue_s = max(max(0.0, float(node.get("busy_until_s", now)) - now),
                      max(0.0, float(node.get("available_after_s", 0))),
                      float(node.get("queue_work_pixels", 0)) / rate if rate > 0 else 0.0)
        compute_s = pixels / rate + float(task.get("estimated_startup_s", 0)) if rate > 0 else 0.0
        start_return = now + (wait or 0) + (transfer or 0) + queue_s + compute_s
        if output_bytes > 0 and node_id != return_node:
            # A receiver advertisement may explicitly supply its reverse-link
            # opportunity.  Otherwise use the received contact forecast only.
            if "return_rate_bytes_s" in node or (not is_local and "transfer_rate_bytes_s" in node):
                rate_back = float(node.get("return_rate_bytes_s", node.get("transfer_rate_bytes_s", 0)))
                wait_back = float(node.get("return_wait_s", node.get("contact_wait_s", 0)))
                if rate_back > 0 and math.isfinite(wait_back):
                    return_time = wait_back + output_bytes / rate_back + float(node.get("return_latency_s", node.get("latency_s", 0)))
                else:
                    reasons.append("no_known_return_contact")
            else:
                rw, rt = _edge_delay(forecast_view, node_id, return_node, output_bytes, start_return)
                if rw is None:
                    reasons.append("no_known_return_contact")
                else:
                    return_time = rw + rt
        total = (wait or 0.0) + (transfer or 0.0) + queue_s + compute_s + return_time
        if not math.isfinite(total):
            reasons.append("nonfinite_cost")
        if task.get("deadline_s") is not None and now + total > float(task["deadline_s"]):
            reasons.append("deadline_infeasible")
        candidates.append({"node_id": node_id, "action": "local" if is_local else "request",
                           "advertisement_timestamp": timestamp, "advertisement_age_s": age,
                           "feasible": not reasons, "rejection_reasons": reasons,
                           "input_bytes": node_input_bytes, "output_bytes": output_bytes,
                           "contact_wait_s": wait, "input_transfer_s": transfer,
                           "queue_s": queue_s, "compute_s": compute_s,
                           "return_s": return_time, "total_s": total if not reasons else None,
                           "observed_resources": {k: node.get(k) for k in ("memory_bytes", "storage_bytes", "energy_j", "compute_pixels_s", "queue_work_pixels", "busy_until_s", "capabilities")}})
    feasible = [c for c in candidates if c["feasible"]]
    chosen = min(feasible, key=lambda x: (x["total_s"], x["node_id"])) if feasible else None
    return {"action": chosen["action"] if chosen else "wait", "destination": chosen["node_id"] if chosen else None,
            "actor_node": actor, "observation_timestamp": local_observation.get("timestamp", now), "now": now,
            "policy_version": POLICY_VERSION, "required_capability": required,
            "candidates": candidates, "candidate_costs": candidates,
            "estimated_cost_s": chosen["total_s"] if chosen else None,
            "reason": "minimum_feasible_local_observation_cost" if chosen else "no_feasible_known_candidate",
            "neighbor_timestamps": {k: v.get("timestamp") for k, v in latest.items()},
            "forecast_id": forecast_view.get("forecast_id"),
            "forecast_expired": now > float(forecast_view.get("valid_until", float("inf"))),
            "admission_required": bool(chosen and chosen["action"] == "request"),
            "step_id": task.get("step_id"), "plan_id": task.get("plan_id"), "mission_id": task.get("mission_id")}
