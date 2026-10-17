"""Independent, fail-closed acceptance checks.

This module intentionally does not import science, statistics, runtime, planning,
forecast, policy or their helpers.  Pixels, hashes and temporal relationships are
recomputed directly.  Tolerances were fixed before inspecting computed results.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
import hashlib
import json
import math

import numpy as np
import rasterio
from affine import Affine
from pyproj import CRS
from rasterio.warp import reproject, Resampling

FLOAT_ATOL = 1e-6
AREA_ATOL_M2 = 1e-6
SCHEMA = "orbitserve-independent-verification-v1"


def _sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1048576), b""):
            h.update(chunk)
    return h.hexdigest()


def _json(path):
    return json.loads(Path(path).read_text())


def _jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


class _Report:
    def __init__(self, kind, **context):
        self.value = {"schema": SCHEMA, "kind": kind, "status": "IN_PROGRESS", "tolerances":
                      {"float_absolute": FLOAT_ATOL, "float_relative": 0, "area_absolute_m2": AREA_ATOL_M2,
                       "classifications": "exact"}, "checks": [], **context}

    def check(self, name, condition, detail=None):
        self.value["checks"].append({"check": name, "status": "PASS" if bool(condition) else "FAIL", "detail": detail})
        return bool(condition)

    def block(self, name, detail):
        self.value["checks"].append({"check": name, "status": "BLOCKED", "detail": detail})

    def finish(self):
        statuses = {row["status"] for row in self.value["checks"]}
        self.value["status"] = "FAIL" if "FAIL" in statuses else "BLOCKED" if "BLOCKED" in statuses else "PASS" if statuses else "BLOCKED"
        return self.value


def _assets(case):
    value = _json(case / "assets.json")
    return value["assets"] if isinstance(value, dict) else value


def _shape_grid(task):
    return (int(task["height"]), int(task["width"])), Affine(*task["transform"][:6]), rasterio.crs.CRS.from_user_input(task["crs"])


def _asset_values(case, asset, grid):
    """Read raw DN once; explicit asset scale/offset override inert GDAL defaults."""
    shape, transform, crs = grid
    with rasterio.open(case / asset["path"]) as ds:
        raw = ds.read(1)
        valid = ds.read_masks(1) != 0
        valid &= np.isfinite(raw)
        if asset.get("nodata") is not None:
            valid &= raw != asset["nodata"]
        scaled = raw.astype(np.float64) * float(asset["scale"]) + float(asset["offset"])
        scaled[~valid] = np.nan
        if raw.shape == shape and ds.transform == transform and ds.crs == crs:
            return scaled
        result = np.full(shape, np.nan, np.float64)
        reproject(scaled, result, src_transform=ds.transform, src_crs=ds.crs,
                  dst_transform=transform, dst_crs=crs, src_nodata=np.nan, dst_nodata=np.nan,
                  resampling=Resampling.nearest if str(asset["band"]).upper() == "SCL" else Resampling.bilinear)
        return result


def _check_assets(report, case, assets):
    for asset in assets:
        path = case / asset.get("path", "__missing__")
        identity = f"{asset.get('phase', '')}:{asset.get('band', path.name)}"
        if not path.is_file():
            report.check("input_exists:" + identity, False, str(path))
            continue
        report.check("input_sha256:" + identity, bool(asset.get("sha256")) and _sha(path) == asset["sha256"], str(path))
        report.check("explicit_scaling:" + identity, all(k in asset for k in ("scale", "offset", "nodata")),
                     {k: asset.get(k) for k in ("scale", "offset", "nodata")})
        acquired_ok = observed_ok = False
        try:
            acquired = datetime.fromisoformat(asset["acquired_at"].replace("Z", "+00:00"))
            acquired_ok = acquired.tzinfo is not None
            if asset.get("observation_time_precision") == "date":
                observed_ok = date.fromisoformat(asset["observation_time"]).isoformat() == asset["observation_time"]
            elif asset.get("observation_time_precision") == "instant":
                observed_ok = datetime.fromisoformat(asset["observation_time"].replace("Z", "+00:00")).tzinfo is not None
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
        report.check("explicit_acquisition_timestamp:" + identity, acquired_ok, asset.get("acquired_at"))
        report.check("explicit_observation_time_and_precision:" + identity, observed_ok,
                     {k: asset.get(k) for k in ("observation_time", "observation_time_precision")})
        with rasterio.open(path) as ds:
            report.check("readable_input:" + identity, ds.count == 1 and ds.width > 0 and ds.height > 0 and ds.crs is not None)
            for key, observed in (("width", ds.width), ("height", ds.height), ("dtype", ds.dtypes[0])):
                if key in asset:
                    report.check("metadata_" + key + ":" + identity, asset[key] == observed, {"declared": asset[key], "actual": observed})
            if "transform" in asset:
                report.check("metadata_transform:" + identity, np.allclose(asset["transform"][:6], tuple(ds.transform)[:6], atol=1e-9, rtol=0))
            if "crs" in asset:
                report.check("metadata_crs:" + identity, rasterio.crs.CRS.from_user_input(asset["crs"]) == ds.crs)
            authority = asset.get("source_raster_band", ds.tags(1))
            authoritative_scale = authority.get("scale", 1 if str(asset["band"]).upper() == "SCL" else None)
            report.check("scale_matches_source_authority:" + identity,
                         authoritative_scale is not None and float(authoritative_scale) == float(asset["scale"]))
            effective_offset = float(authority.get("offset", 0))
            if "source_raster_band" in asset and str(asset["band"]).upper() != "SCL":
                # Provider maintainer: earthsearch:boa_offset_applied is the
                # authoritative harmonization flag when raster:bands is stale.
                harmonized = asset.get("earthsearch_boa_offset_applied")
                report.check("explicit_COG_harmonization_authority:" + identity,
                             isinstance(harmonized, bool) and asset.get("scaling_version") == "earthsearch-boa-v1"
                             and asset.get("scaling_authority_url") == "https://github.com/Element84/earth-search/issues/9"
                             and asset.get("reported_offset") == authority.get("offset", 0)
                             and asset.get("reported_scale") == float(authoritative_scale),
                             {"boa_offset_applied": harmonized,"declared_offset": authority.get("offset"),"effective_offset": asset["offset"]})
                effective_offset = 0.0 if harmonized is True else effective_offset
            report.check("offset_matches_source_authority:" + identity,
                         effective_offset == float(asset["offset"]))
            if str(asset["band"]).upper() != "SCL":
                pixels = ds.read(1, masked=True).compressed()
                # These inputs explicitly retain DN. Physical reflectance or
                # temperature pre-scaling would violate this metadata contract.
                report.check("unscaled_DN_present:" + identity,
                             pixels.size > 0 and float(np.nanmax(np.abs(pixels))) > 2,
                             {"raw_max_abs": float(np.nanmax(np.abs(pixels))) if pixels.size else None,
                              "scaling_applied": asset.get("scaling_applied", False)})
                report.check("no_preapplied_scale:" + identity, asset.get("scaling_applied", False) is False)


def _read_raster(report, output, filename, grid=None):
    path = output / filename
    if not report.check("product_exists:" + filename, path.is_file()):
        return None
    with rasterio.open(path) as ds:
        arr = ds.read(1)
        if np.issubdtype(arr.dtype, np.floating):
            arr = arr.astype(np.float64)
            arr[ds.read_masks(1) == 0] = np.nan
        if grid:
            shape, transform, crs = grid
            report.check("product_grid:" + filename, arr.shape == shape and ds.transform == transform and ds.crs == crs)
        return arr


def _same_float(report, name, observed, expected):
    if observed is None:
        return
    if observed.shape != expected.shape:
        report.check(name, False, {"actual_shape": list(observed.shape), "expected_shape": list(expected.shape)})
        return
    finite = np.isfinite(expected)
    same_valid = np.array_equal(np.isfinite(observed), finite)
    maxdiff = float(np.max(np.abs(observed[finite].astype(np.float64) - expected[finite]))) if finite.any() else 0.0
    report.check(name, same_valid and maxdiff <= FLOAT_ATOL, {"max_absolute_error": maxdiff, "finite_mask_equal": same_valid})


def _same_class(report, name, observed, expected):
    if observed is not None:
        report.check(name, observed.shape == expected.shape and np.array_equal(observed, expected),
                     {"mismatched_pixels": int(np.count_nonzero(observed != expected)) if observed.shape == expected.shape else None})


def _index(a, b):
    denominator = a + b
    good = np.isfinite(a) & np.isfinite(b) & (denominator != 0)
    result = np.full(a.shape, np.nan, np.float64)
    result[good] = (a[good] - b[good]) / denominator[good]
    return result


def _pixel_area(transform, crs):
    projected = CRS.from_user_input(crs)
    if not projected.is_projected:
        raise ValueError("Fixed V0 area verification requires a projected CRS, never square degrees")
    axes = projected.axis_info
    if len(axes) < 2:
        raise ValueError("CRS is missing projected-unit conversion")
    return abs(transform.a * transform.e - transform.b * transform.d) * axes[0].unit_conversion_factor * axes[1].unit_conversion_factor


def verify_science(case_dir, output_dir):
    """Recompute science from input assets, never from author statistics helpers."""
    case, output = Path(case_dir), Path(output_dir)
    report = _Report("science", case_dir=str(case), output_dir=str(output))
    try:
        task, assets = _json(case / "task.json"), _assets(case)
        _check_assets(report, case, assets)
        bands = {str(a["band"]).upper() for a in assets}
        if {"NDVI", "LST"} <= bands:
            _verify_tvdi(report, case, output, task, assets)
        else:
            _verify_water(report, case, output, task, assets)
    except (OSError, ValueError, KeyError, TypeError, IndexError, rasterio.errors.RasterioError) as exc:
        report.check("verification_completed", False, f"{type(exc).__name__}: {exc}")
    return report.finish()


def _verify_water(report, case, output, task, assets):
    grid = _shape_grid(task)
    shape, transform, crs = grid
    report.check("fixed_grid_20m", shape == (250, 250) and abs(transform.a) == 20 and abs(transform.e) == 20)
    values = {}
    for phase in ("before", "after"):
        selected = {str(a["band"]).upper(): a for a in assets if a.get("phase") == phase}
        report.check("required_bands:" + phase, {"B02", "B03", "B04", "B08", "B11", "SCL"} <= selected.keys())
        data = {band: _asset_values(case, asset, grid) for band, asset in selected.items()}
        indices = {"ndvi": _index(data["B08"], data["B04"]),
                   "ndwi": _index(data["B03"], data["B08"]),
                   "mndwi": _index(data["B03"], data["B11"])}
        values[phase] = {"data": data, "indices": indices, "assets": selected}
    _verify_water_products(report, output, task, grid, values)


def _verify_water_products(report, output, task, grid, values):
    valid, water = {}, {}
    for phase in ("before", "after"):
        d = values[phase]["data"]
        blue, green, red, nir, swir = (d[k] for k in ("B02", "B03", "B04", "B08", "B11"))
        indices = values[phase]["indices"]
        valid[phase] = np.isin(d["SCL"], [4, 5, 6, 7])
        for band in (blue, green, red, nir, swir):
            valid[phase] &= np.isfinite(band)
        valid[phase] &= np.isfinite(indices["mndwi"])
        water[phase] = np.where(valid[phase], indices["mndwi"] > 0, 255).astype(np.uint8)
        _same_class(report, phase + "_valid_mask", _read_raster(report, output, "valid_" + phase + ".tif", grid), valid[phase].astype(np.uint8))
        _same_class(report, phase + "_water_mask", _read_raster(report, output, phase + "_water.tif", grid), water[phase])
        for name, calculated in indices.items():
            expected = np.where(valid[phase], calculated, np.nan).astype(np.float32)
            _same_float(report, phase + "_" + name + "_from_raw_DN", _read_raster(report, output, phase + "_" + name + ".tif", grid), expected)
    common = valid["before"] & valid["after"]
    expected = np.zeros(common.shape, np.uint8)
    expected[common & (water["before"] == 0) & (water["after"] == 0)] = 1
    expected[common & (water["before"] == 1) & (water["after"] == 1)] = 2
    expected[common & (water["before"] == 0) & (water["after"] == 1)] = 3
    expected[common & (water["before"] == 1) & (water["after"] == 0)] = 4
    _same_class(report, "common_valid_intersection", _read_raster(report, output, "valid_common.tif", grid), common.astype(np.uint8))
    actual = _read_raster(report, output, "change.tif", grid)
    _same_class(report, "five_exact_change_classes", actual, expected)
    report.check("common_valid_nonempty", bool(common.any()))
    stats = _json(output / "stats.json")
    area = _pixel_area(grid[1], grid[2])
    labels = ("unknown", "stable_nonwater", "stable_water", "gained", "lost")
    counts = {name: int(np.count_nonzero(expected == i)) for i, name in enumerate(labels)}
    report.check("class_partition_conservation", sum(counts.values()) == expected.size and sum(counts[k] for k in labels[1:]) == int(common.sum()))
    report.check("statistics_class_counts", stats.get("class_counts") == counts, counts)
    expected_stats = {"total_pixels": expected.size, "valid_pixels": int(common.sum()), "valid_common_pixels": int(common.sum()),
                      "unknown_pixels": counts["unknown"], "valid_coverage_fraction": float(common.mean()),
                      "pixel_area_m2": area, "aoi_area_m2": area * expected.size,
                      "common_valid_area_m2": area * int(common.sum()), "unknown_area_m2": area * counts["unknown"],
                      "gained_pixels": counts["gained"], "lost_pixels": counts["lost"],
                      "gained_area_m2": counts["gained"] * area, "lost_area_m2": counts["lost"] * area,
                      "net_change_area_m2": (counts["gained"] - counts["lost"]) * area}
    for phase in ("before", "after"):
        expected_stats.update({phase + "_valid_pixels": int(valid[phase].sum()),
                               phase + "_valid_coverage_fraction": float(valid[phase].mean()),
                               phase + "_water_common_pixels": int(((water[phase] == 1) & common).sum()),
                               phase + "_water_observed_pixels": int((water[phase] == 1).sum())})
    for key, value in expected_stats.items():
        tolerance = AREA_ATOL_M2 if key.endswith("m2") else 1e-12 if isinstance(value, float) else 0
        observed = stats.get(key)
        report.check("statistics:" + key, observed is not None and abs(observed - value) <= tolerance,
                     {"expected": value, "actual": observed})
    for label, count in counts.items():
        actual_area = stats.get("class_area_m2", {}).get(label)
        report.check("class_area:" + label, actual_area is not None and abs(actual_area - count * area) <= AREA_ATOL_M2)
    # Vector attributes must conserve independently counted gained/lost pixels.
    vector = _json(output / "changes.geojson")
    report.check("geojson_feature_collection", vector.get("type") == "FeatureCollection")
    vector_counts = defaultdict(int)
    for feature in vector.get("features", []):
        prop = feature["properties"]
        code = int(prop["class_code"])
        vector_counts[code] += int(prop["pixel_count"])
        report.check("vector_feature_area", code in (3,4) and abs(float(prop["area_m2"]) - int(prop["pixel_count"]) * area) <= AREA_ATOL_M2)
        report.check("vector_geometry", feature.get("geometry", {}).get("type") in ("Polygon", "MultiPolygon"))
    report.check("vector_change_conservation", vector_counts[3] == counts["gained"] and vector_counts[4] == counts["lost"])
    from rasterio.features import rasterize
    from rasterio.warp import transform_geom
    geometries = [(transform_geom("EPSG:4326", grid[2], f["geometry"]), int(f["properties"]["class_code"])) for f in vector.get("features", [])]
    rasterized = rasterize(geometries, out_shape=expected.shape, transform=grid[1], fill=0, dtype="uint8") if geometries else np.zeros(expected.shape, np.uint8)
    _same_class(report, "vector_geometry_matches_change_pixels", rasterized, np.where(np.isin(expected, (3,4)), expected, 0).astype(np.uint8))
    _check_png(report, output / "map.png")
    _verify_rgb_overlays(report, output, grid, values, valid, water, common, expected, stats)
    report.value["independent_statistics"] = {**expected_stats, "class_counts": counts}


def _verify_rgb_overlays(report, output, grid, values, valid, water, common, changes, stats):
    """Recreate display pixels from independently decoded source reflectance.

    Neither science helpers nor its encoded RGB/PNG values are used to produce
    the expected panels. The fixed display mapping is a declared output contract,
    not a new scientific threshold or a per-tile contrast estimate.
    """
    from PIL import Image
    from rasterio.enums import ColorInterp
    shape, transform, crs = grid
    expected_rgb = {}
    display = stats.get("rgb_display", {})
    report.check("rgb_fixed_physical_display_contract",
                 display.get("bands") == ["B04", "B03", "B02"]
                 and display.get("reflectance_min") == 0.0
                 and display.get("reflectance_max") == 0.30
                 and display.get("gamma") == 1.0
                 and display.get("water_overlay") == {"color": [35,108,177], "opacity": 0.35}
                 and display.get("change_overlay") == {"gained_color": [20,169,145], "lost_color": [218,89,56], "opacity": 0.50}
                 and display.get("unknown_color") == [165,165,165], display)
    for phase in ("before", "after"):
        expected = np.zeros((4, *shape), dtype=np.uint8)
        for channel, band in enumerate(("B04", "B03", "B02")):
            physical = values[phase]["data"][band]
            display_value = np.rint(np.clip(physical[valid[phase]] / 0.30, 0, 1) * 255)
            expected[channel, valid[phase]] = display_value.astype(np.uint8)
        expected[3, valid[phase]] = 255
        expected_rgb[phase] = expected
        path = output / (phase + "_rgb.tif")
        if not report.check("rgb_material_product:" + phase, path.is_file()):
            continue
        with rasterio.open(path) as ds:
            report.check("rgb_georeferenced_rgba:" + phase,
                         ds.count == 4 and ds.dtypes == ("uint8",) * 4 and ds.nodata is None
                         and ds.shape == shape and ds.crs == crs and ds.transform == transform
                         and ds.colorinterp == (ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha)
                         and ds.descriptions == ("B04", "B03", "B02", "phase-valid alpha"))
            report.check("rgb_declared_mapping:" + phase, json.loads(ds.tags().get("rgb_display", "null")) == display)
            _same_class(report, "rgb_from_source_physical_values:" + phase, ds.read(), expected)
        # Source records must contain the exact source and scaling identity for
        # each retained tile. There must be no source-free decorative backdrop.
        provenance = stats.get("tile_provenance", [])
        report.check("rgb_tile_provenance_present:" + phase, bool(provenance))
        for index, tile in enumerate(provenance):
            for band in ("B04", "B03", "B02"):
                matching = [p for p in tile.get("input_scaling", []) if p.get("phase") == phase and p.get("band") == band]
                source = values[phase]["assets"][band]
                report.check("rgb_source_record:" + phase + ":" + band + ":" + str(index),
                             len(matching) == 1 and matching[0].get("source") == source["source"]
                             and matching[0].get("source_sha256") == source["sha256"]
                             and matching[0].get("path") == source["path"]
                             and matching[0].get("item_id") == source.get("item_id")
                             and matching[0].get("scale_applied_once") == source["scale"]
                             and matching[0].get("offset_applied_once") == source["offset"])
    with Image.open(output / "map.png") as image:
        report.check("overlay_png_canvas", image.mode == "RGB" and image.size == (1000,480))
        pixels = np.asarray(image.convert("RGB"))
    for index, phase in enumerate(("before", "after", "change")):
        base_phase = "after" if phase == "change" else phase
        panel = np.transpose(expected_rgb[base_phase][:3], (1,2,0)).copy()
        if phase == "change":
            panel_valid = common
            layers = [(changes == 3, (20,169,145), 0.5), (changes == 4, (218,89,56), 0.5)]
        else:
            panel_valid = valid[phase]
            layers = [(water[phase] == 1, (35,108,177), 0.35)]
        for selected, color, alpha in layers:
            mask = selected & panel_valid
            for channel in range(3):
                channel_pixels = panel[:,:,channel]
                channel_pixels[mask] = np.rint(channel_pixels[mask] * (1-alpha) + color[channel] * alpha).astype(np.uint8)
        panel[~panel_valid] = (165,165,165)
        wanted = np.asarray(Image.fromarray(panel).resize((300,300), Image.Resampling.NEAREST))
        x = 20 + index * 330
        actual = pixels[70:370, x:x+300]
        _same_class(report, "overlay_panel_from_source_pixels:" + phase, actual, wanted)


def _ratio_independent(numerator, denominator):
    result = np.full(numerator.shape, np.nan, np.float64)
    valid = np.isfinite(numerator) & np.isfinite(denominator) & (denominator != 0)
    result[valid] = numerator[valid] / denominator[valid]
    result[~np.isfinite(result)] = np.nan
    return result


def _check_png(report, path):
    from PIL import Image
    with Image.open(path) as im:
        im.verify()
    with Image.open(path) as im:
        report.check("png_decodes", im.format == "PNG" and im.width >= 250 and im.height >= 250)


def _verify_tvdi(report, case, output, task, assets):
    grid = _shape_grid(task)
    by_band = {a["band"].upper(): a for a in assets}
    ndvi = _asset_values(case, by_band["NDVI"], grid)
    lst = _asset_values(case, by_band["LST"], grid)
    _same_float(report, "NDVI_scaled_once", _read_raster(report, output, "ndvi.tif", grid), ndvi.astype(np.float32))
    _same_float(report, "LST_scaled_once_float32_storage", _read_raster(report, output, "lst.tif", grid), lst.astype(np.float32))
    accepted = np.isfinite(ndvi) & np.isfinite(lst) & (ndvi >= 0) & (ndvi <= 1) & (lst > 0)
    report.check("sufficient_physical_input_pairs", int(accepted.sum()) >= 100)
    x, y = ndvi[accepted], lst[accepted]
    edges = np.linspace(float(x.min()), float(x.max()), 101)
    centers, lows, highs, counts = [], [], [], []
    # Deliberately use explicit bin predicates and scalar OLS rather than the
    # author's searchsorted/ufunc.at/polyfit implementation.
    for i, (lo, hi) in enumerate(zip(edges[:-1], edges[1:])):
        member = (x >= lo) & ((x <= hi) if i == 99 else (x < hi))
        count = int(member.sum())
        counts.append(count)
        if count:
            centers.append((lo + hi) / 2)
            lows.append(float(y[member].min()))
            highs.append(float(y[member].max()))
    report.check("TVDI_bin_partition", sum(counts) == len(x) and len(centers) >= 2)
    c = np.array(centers)
    def ols(z):
        z = np.array(z)
        centered = c - c.mean()
        slope = float(np.dot(centered, z-z.mean()) / np.dot(centered, centered))
        return slope, float(z.mean() - slope*c.mean())
    dry_slope, dry_intercept = ols(highs)
    wet_slope, wet_intercept = ols(lows)
    wet = ndvi*wet_slope+wet_intercept
    span = ndvi*dry_slope+dry_intercept-wet
    valid = accepted & np.isfinite(span) & (span > 0)
    expected = np.full(ndvi.shape, np.nan, np.float64)
    expected[valid] = np.clip((lst[valid]-wet[valid])/span[valid], 0, 1)
    _same_float(report, "TVDI_from_independent_edge_fit", _read_raster(report, output, "tvdi.tif", grid), expected.astype(np.float32))
    _same_class(report, "TVDI_valid", _read_raster(report, output, "valid.tif", grid), valid.astype(np.uint8))
    classified = np.where(valid, expected > 0.75, 255).astype(np.uint8)
    _same_class(report, "TVDI_above_threshold", _read_raster(report, output, "above_threshold.tif", grid), classified)
    stats = _json(output / "stats.json")
    fit = stats.get("fit", {})
    report.check("TVDI_fixed_100_bins", fit.get("n_bins") == 100)
    report.check("TVDI_bin_counts", fit.get("bin_counts") == counts)
    for name, val in (("dry_slope", dry_slope), ("dry_intercept", dry_intercept), ("wet_slope", wet_slope), ("wet_intercept", wet_intercept)):
        actual = fit.get(name)
        report.check("TVDI_fit:" + name, actual is not None and abs(actual-val) <= FLOAT_ATOL, {"actual": actual, "independent": val})
    count_valid, count_above = int(valid.sum()), int((classified == 1).sum())
    independently = {"valid_pixels": count_valid, "valid_input_pairs": int(accepted.sum()),
                     "total_pixels": ndvi.size, "unknown_pixels": ndvi.size-count_valid,
                     "pixels_above_threshold": count_above, "threshold": 0.75,
                     "percentage_above_threshold": 100*count_above/count_valid,
                     "fraction_above_threshold": count_above/count_valid,
                     "valid_coverage_fraction": count_valid/ndvi.size}
    for key, val in independently.items():
        observed = stats.get(key)
        report.check("TVDI_statistics:" + key, observed is not None and abs(observed-val) <= (1e-12 if isinstance(val,float) else 0), {"actual": observed, "independent": val})
    _check_png(report, output / "map.png")
    report.value["independent_statistics"] = independently
    report.value["scientific_limitations"] = ["TVDI reproduced as a region-relative empirical indicator, not independently validated drought accuracy.", "Inputs are supplied NDVI and LST products; no spectral NDVI recomputation is possible from these bundles."]


def compare_products(a, b):
    """Compare independently opened rasters, categories exact, float atol=1e-6."""
    a, b = Path(a), Path(b)
    report = _Report("product_equivalence", a=str(a), b=str(b))
    try:
        left = {str(p.relative_to(a)) for p in a.rglob("*.tif")}
        right = {str(p.relative_to(b)) for p in b.rglob("*.tif")}
        report.check("nonempty_matching_raster_inventory", bool(left) and left == right, {"left": sorted(left), "right": sorted(right)})
        for name in sorted(left & right):
            with rasterio.open(a / name) as da, rasterio.open(b / name) as db:
                report.check("grid:" + name, da.shape == db.shape and da.count == db.count and da.crs == db.crs and da.transform == db.transform)
                x, y = da.read(), db.read()
                if np.issubdtype(x.dtype, np.floating) or np.issubdtype(y.dtype, np.floating):
                    _same_float(report, "pixels:" + name, x, y)
                else:
                    _same_class(report, "pixels:" + name, x, y)
        for name in ("stats.json",):
            if (a / name).is_file() or (b / name).is_file():
                ignore = {"tile_count", "tile_provenance", "implementation_version"}
                left_stats = {k:v for k,v in _json(a / name).items() if k not in ignore}
                right_stats = {k:v for k,v in _json(b / name).items() if k not in ignore}
                report.check("statistics:" + name, left_stats == right_stats)
    except (OSError, ValueError, KeyError, TypeError, rasterio.errors.RasterioError) as exc:
        report.check("comparison_completed", False, f"{type(exc).__name__}: {exc}")
    return report.finish()


def _safe_key(value):
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def _check_file_rows(report, root, rows, label):
    report.check(label + ":nonempty_manifest", bool(rows))
    for row in rows:
        relative = Path(row["path"])
        if not report.check(label + ":safe_relative_path", not relative.is_absolute() and ".." not in relative.parts, str(relative)):
            continue
        path = root / relative
        exists = path.is_file() and not path.is_symlink()
        report.check(label + ":file:" + str(relative), exists and path.stat().st_size == row["size_bytes"] and _sha(path) == row["sha256"], str(path))


def _closure_intervals(events):
    result = []
    for e in events:
        if e.get("event") == "ground_link_closed":
            result.append((float(e["start_s"]), float(e["end_s"]), None))
        if e.get("event") == "data_link_closed":
            result.append((float(e["start_s"]), float(e["end_s"]), {e["source"], e["destination"]}))
    return result


def _edge_closed(intervals, src, dst, at):
    return any(a <= at < b and ((edge is None and "G" in (src, dst)) or edge == {src, dst}) for a,b,edge in intervals)


def _verify_harness(report, run, calls, manifest):
    jobs = defaultdict(list)
    for call in calls:
        jobs[call["job_id"]].append(call)
    report.check("harness_has_jobs", bool(jobs))
    for job, rows in jobs.items():
        states = {r["status"]: r for r in rows}
        previous_resources = None
        for row in sorted(rows, key=lambda r: r["timestamp"]):
            observed = row.get("resource_observation", {})
            fields = ("max_rss_bytes", "user_cpu_s", "system_cpu_s")
            numeric = all(isinstance(observed.get(k), (int, float))
                          and math.isfinite(observed[k]) and observed[k] >= 0 for k in fields)
            report.check("harness_real_resource_observation:" + job + ":" + row["status"],
                         numeric and observed.get("scope") == "baseline_host_process_not_simulated_satellite"
                         and observed.get("process_id") == row.get("process_id")
                         and isinstance(observed.get("observed_at"), (int,float))
                         and 0 <= observed["observed_at"] <= row["timestamp"]
                         and observed.get("method") == "Linux getrusage(RUSAGE_SELF); RSS high-water mark")
            if numeric and previous_resources:
                report.check("harness_monotonic_resource_counters:" + job + ":" + row["status"],
                             all(observed[k] >= previous_resources[k] for k in fields))
            if numeric:
                previous_resources = observed
        report.check("harness_lifecycle:" + job, {"accepted", "running", "succeeded"} <= states.keys() and "failed" not in states)
        if not {"accepted", "running", "succeeded"} <= states.keys():
            continue
        report.check("harness_order:" + job, states["accepted"]["timestamp"] <= states["running"]["timestamp"] < states["succeeded"]["timestamp"])
        inputs = states["running"].get("input_manifest", [])
        declared = [r["files"] for r in manifest["inputs"] if r["case_id"] == states["running"]["mission_id"]]
        report.check("harness_inputs_match_manifest:" + job, len(declared) == 1 and inputs == declared[0])
        _check_file_rows(report, Path(states["succeeded"]["output_dir"]), states["succeeded"]["output_artifacts"], "harness_output:" + job)


def verify_run(run_dir):
    """Validate material files and correlated process/transfer/job evidence."""
    run = Path(run_dir)
    report = _Report("run_evidence", run_dir=str(run))
    try:
        manifest = _json(run / "run_manifest.json")
        report.check("manifest_identity", manifest.get("run_id") == run.name)
        calls = _jsonl(run / "tool_calls.jsonl")
        if not (run / "runtime_result.json").is_file():
            if manifest.get("experiment") in ("E0", "W02"):
                _verify_harness(report, run, calls, manifest)
            else:
                report.block("runtime_result", "No completed distributed runtime evidence")
            return report.finish()
        result = _json(run / "runtime_result.json")
        config = _json(run / "node_config.json")
        events = _jsonl(run / "node_events.jsonl")
        transfers = _jsonl(run / "transfers.jsonl")
        controls = _jsonl(run / "control_messages.jsonl")
        decisions = _jsonl(run / "decisions.jsonl")
        plan = _json(run / "plan.json")
        report.check("delivered_status_with_receipt", result.get("status") == "delivered" and bool(result.get("delivery")))
        report.check("no_runtime_worker_failures", not [e for e in events if e.get("event") in ("runtime_failed", "worker_failed")])
        _verify_processes(report, run, result, config, events)
        verified = _verify_transfers(report, run, transfers, controls, events, config)
        _verify_jobs(report, run, calls, plan, verified)
        _verify_worker_capacity(report, calls, config, plan)
        _verify_decisions(report, decisions, controls, events, config)
        _verify_dag(report, plan, calls)
        delivered = [e for e in events if e.get("event") == "delivered" and e.get("node_id") == "G"]
        report.check("one_ground_delivery", len(delivered) == 1)
        if len(delivered) == 1:
            receipt = delivered[0]
            tid = receipt["delivery_transfer_id"]
            vr = [v for v in verified if v["transfer_id"] == tid and v["destination"] == "G"]
            report.check("ground_received_before_delivered", len(vr) == 1 and vr[0]["timestamp"] <= receipt["timestamp"])
            output = Path(result["outputs_dir"])
            report.check("output_in_ground_root", output.resolve().is_relative_to((run / "nodes" / "G").resolve()))
            _check_file_rows(report, output, receipt["files"], "ground_delivery")
            science = verify_science(run / "nodes" / "A" / "case", output)
            report.check("ground_science_independently_recomputed", science["status"] == "PASS", science)
        closures = _closure_intervals(events)
        for event in controls:
            if event.get("event") == "delivered":
                report.check("control_respects_closure", not _edge_closed(closures, event["source"], event["destination"], event["timestamp"]),
                             {k:event.get(k) for k in ("message_id","source","destination","timestamp")})
        if result.get("scenario") == "ground_outage":
            _verify_ground_outage(report, events, calls, controls)
        if result.get("scenario") == "transfer_outage":
            _verify_transfer_outage(report, events, transfers, calls)
        report.value["run_id"] = run.name
        report.value["scenario"] = result.get("scenario")
        report.value["execution_nodes"] = {e["step_id"]: e["node_id"] for e in calls if e.get("status") == "running"}
    except (OSError, ValueError, KeyError, TypeError, IndexError, rasterio.errors.RasterioError) as exc:
        report.check("run_verification_completed", False, f"{type(exc).__name__}: {exc}")
    return report.finish()


def _verify_processes(report, run, result, config, events):
    started = [e for e in events if e.get("event") == "worker_started"]
    nodes = {e["node_id"] for e in started}
    report.check("four_actual_workers", nodes == {"A", "B", "C", "G"} and len(started) == 4)
    for key in ("host_pid", "pid_namespace", "net_namespace", "mount_namespace", "policy_instance"):
        values = [e.get(key) for e in started]
        report.check("four_distinct:" + key, len(values) == 4 and all(v is not None for v in values) and len(set(values)) == 4, values)
    proof = _json(run / "isolation.json")
    report.check("isolation_actual_negative_probes", proof.get("node_writable") is True and proof.get("source_readonly") is True
                 and proof.get("readonly_errno") == 30 and proof.get("network_denied") is True
                 and proof.get("network_errno") in (1, 13, 101, 113)
                 and proof.get("host_workspace_visible") is False and proof.get("raw_data_visible") is False
                 and proof.get("other_nodes_visible") is False)
    for worker in started:
        mount_rows = [line.split() for line in worker.get("mountinfo", "").splitlines()]
        roots = [row for row in mount_rows if len(row) > 6 and row[4] == "/node"]
        apps = [row for row in mount_rows if len(row) > 6 and row[4] == "/app"]
        expected_root = str((run / "nodes" / worker["node_id"]).resolve())
        report.check("isolated_own_mount:" + worker["node_id"], len(roots) == 1 and roots[0][3] == expected_root and "rw" in roots[0][5].split(","))
        report.check("readonly_code_mount:" + worker["node_id"], len(apps) == 1 and "ro" in apps[0][5].split(","))
        report.check("no_peer_or_evaluation_mount:" + worker["node_id"], not any("orbitserve-data" in row[3] or "/evaluation" in row[3] or ("/nodes/" in row[3] and row[3] != expected_root) for row in mount_rows if len(row) > 6))
    report.check("only_A_source_preload", (run/"nodes/A/case").is_dir() and not any((run/"nodes"/n/"case").exists() for n in ("B","C","G")))
    declared = _json(run/"run_manifest.json")["inputs"]
    report.check("single_declared_source_case", len(declared) == 1)
    if len(declared) == 1:
        _check_file_rows(report, run/"nodes/A/case", declared[0]["files"], "original_source_preload")
    report.check("workers_clean_exit", set(result.get("processes", {})) == nodes and all(v.get("exit_code") == 0 for v in result.get("processes", {}).values()))


def _verify_transfers(report, run, transfers, controls, events, config):
    groups = defaultdict(list)
    for e in transfers:
        if e.get("event") not in ("capacity_budget", "capacity_charge") and e.get("transfer_id"):
            groups[e["transfer_id"]].append(e)
    report.check("actual_crossnode_transfers", bool(groups))
    closures = _closure_intervals(events)
    verified = []
    wire_by_edge = defaultdict(list)
    for tid, rows in groups.items():
        starts = [r for r in rows if r.get("event") == "started"]
        received = [r for r in rows if r.get("event") == "verified"]
        report.check("transfer_lifecycle:" + tid, len(starts) == 1 and len(received) == 1 and any(r.get("event") == "completed" for r in rows))
        if len(starts) != 1 or len(received) != 1:
            continue
        start, receipt = starts[0], received[0]
        verified.append(receipt)
        source = run/"nodes"/start["source"]/"outgoing"/(_safe_key(start["artifact_id"])+".zip")
        report.check("actual_transferred_archive:" + tid, source.is_file() and source.stat().st_size == start["size_bytes"] and _sha(source) == start["sha256"])
        report.check("received_hash_and_size:" + tid, receipt.get("received_sha256") == start["sha256"] and receipt.get("received_bytes") == start["size_bytes"] and receipt.get("computable") is True)
        destination = run/"nodes"/start["destination"]/"artifacts"/_safe_key(start["artifact_id"])
        _check_file_rows(report, destination, start["files"], "received:"+tid)
        # Source ZIP contents and destination hashes are checked separately from
        # their two log claims, proving retained bytes are the actual payload.
        import zipfile
        with zipfile.ZipFile(source) as archive:
            expected = {r["path"]:r for r in start["files"]}
            report.check("archive_inventory:"+tid, set(archive.namelist()) == set(expected))
            for name, item in expected.items():
                data = archive.read(name)
                report.check("archive_member_hash:"+tid+":"+name, len(data) == item["size_bytes"] and hashlib.sha256(data).hexdigest() == item["sha256"])
        chunks = [r for r in rows if r.get("event") == "chunk"]
        cursor = 0
        with source.open("rb") as stream:
            for chunk in chunks:
                count = chunk["chunk_bytes"]
                data = stream.read(count)
                report.check("contiguous_payload_chunk:"+tid, chunk["offset"] == cursor and chunk["end_offset"] == cursor+count and len(data) == count and hashlib.sha256(data).hexdigest() == chunk["chunk_sha256"])
                cursor += count
                report.check("data_respects_closure:"+tid, not _edge_closed(closures, start["source"], start["destination"], chunk["timestamp"]))
                report.check("monotonic_chunk_time:"+tid, start["timestamp"] <= chunk["transmission_start"] <= chunk["transmission_end"] <= receipt["timestamp"])
                wire_by_edge[(start["source"],start["destination"])].append((chunk["timestamp"],count))
        report.check("payload_byte_conservation:"+tid, cursor == start["size_bytes"] and chunks[-1]["timestamp"] <= receipt["timestamp"])
    for row in controls:
        if row.get("event") == "delivered":
            wire_by_edge[(row["source"],row["destination"])].append((row["timestamp"],row["size_bytes"]))
    _verify_capacity_charges(report, transfers, controls, closures, config)
    return verified


def _verify_jobs(report, run, calls, plan, verified):
    groups = defaultdict(list)
    for event in calls:
        groups[event["job_id"]].append(event)
    report.check("actual_jobs_exist", bool(groups))
    for job, rows in groups.items():
        states = {r["status"]:r for r in rows}
        report.check("job_lifecycle:"+job, {"accepted","queued","running","succeeded"} <= states.keys() and "failed" not in states)
        if not {"accepted","queued","running","succeeded"} <= states.keys():
            continue
        start, end = states["running"], states["succeeded"]
        report.check("job_time_order:"+job, states["accepted"]["timestamp"] <= states["queued"]["timestamp"] <= start["timestamp"] < end["timestamp"])
        report.check("one_execution_per_job:"+job, sum(r["status"] == "running" for r in rows) == 1 and len({r["node_id"] for r in rows}) == 1)
        inputs = start.get("input_artifacts", [])
        report.check("job_input_inventory:"+job, {a["artifact_id"] for a in inputs} == set(start["input_artifact_ids"]) and bool(inputs))
        for artifact in inputs:
            arrival = [v for v in verified if v["destination"] == start["node_id"] and v["artifact_id"] == artifact["artifact_id"] and v["timestamp"] <= start["timestamp"]]
            local = [a for previous in calls if previous.get("status") == "succeeded" and previous["node_id"] == start["node_id"] and previous["timestamp"] < start["timestamp"] for a in previous.get("output_artifacts", []) if a["artifact_id"] == artifact["artifact_id"]]
            remote_ok = bool(arrival) and artifact["files"] == arrival[-1]["files"]
            local_ok = bool(local) and artifact["files"] == local[-1]["files"] and artifact["sha256"] == local[-1]["sha256"]
            preloaded = start["node_id"] == "A" and artifact["artifact_id"] == "case" and artifact.get("source") == "preloaded_source" and artifact["files"] == _json(run/"run_manifest.json")["inputs"][0]["files"]
            report.check("input_verified_before_execution:"+job+":"+artifact["artifact_id"], (remote_ok or local_ok or preloaded) and artifact["verified_at"] <= start["timestamp"], {"provenance": "received_verified_transfer" if remote_ok else "prior_same_node_tool_success" if local_ok else "original_A_source_preload" if preloaded else "missing"})
        output_root = run/"nodes"/start["node_id"]/"jobs"/_safe_key(job)
        for artifact in end.get("output_artifacts", []):
            _check_file_rows(report, output_root, artifact["files"], "tool_output:"+job)
        report.check("tool_output_materialized:"+job, bool(end.get("output_artifacts")))
        step = next((s for s in plan["steps"] if s["step_id"] == start["step_id"]), {})
        obs = states["accepted"].get("resource_observation", {})
        report.check("admitted_with_sufficient_resources:"+job, step.get("estimated_memory_bytes",math.inf) <= obs.get("memory_bytes",-1) and step.get("estimated_storage_bytes",math.inf) <= obs.get("storage_bytes",-1) and step.get("estimated_energy_j",math.inf) <= obs.get("energy_j",-1))


def _verify_decisions(report, decisions, controls, events, config):
    started = {e["node_id"]:e for e in events if e.get("event") == "worker_started"}
    delivered_ads = {e.get("message_id"):e for e in controls if e.get("event") == "delivered" and e.get("message_type") in ("advertisement", "request_receipt") and "observation" in e.get("payload", {})}
    report.check("node_local_policy_decisions_exist", bool(decisions))
    resource_effect = capability_effect = False
    for d in decisions:
        actor = d.get("actor_node")
        report.check("decision_actor_process", actor in started and d.get("pid") == started[actor].get("namespace_pid"))
        obs = d["local_observation"]
        report.check("decision_local_observation", obs["node_id"] == actor and obs["timestamp"] <= d["timestamp"])
        for ad in d.get("received_neighbors", []):
            mid = ad.get("message_id")
            evidence = delivered_ads.get(mid)
            report.check("neighbor_ad_received_before_decision", evidence is not None and evidence["destination"] == actor and evidence["source"] == ad["node_id"] and evidence["timestamp"] <= d["timestamp"] and evidence["payload"]["observation"]["timestamp"] == ad["timestamp"])
        decision = d["decision"]
        candidates = decision["candidates"]
        feasible = [c for c in candidates if c["feasible"]]
        if feasible:
            best = min(feasible, key=lambda c:(c["total_s"],c["node_id"]))
            report.check("local_minimum_feasible_cost", decision["destination"] == best["node_id"])
            for c in candidates:
                reasons = c["rejection_reasons"]
                r = c["observed_resources"]
                if "insufficient_memory" in reasons:
                    valid_reason = r["memory_bytes"] < d["task"]["estimated_memory_bytes"]
                    report.check("memory_rejection_real", valid_reason)
                    resource_effect |= valid_reason and decision["destination"] != c["node_id"]
                if "missing_capability" in reasons:
                    need = decision["required_capability"]
                    valid_reason = need not in r["capabilities"] and not (need in ("water_tile","water_merge") and "water_analysis" in r["capabilities"])
                    report.check("capability_rejection_real", valid_reason)
                    capability_effect |= valid_reason and decision["destination"] != c["node_id"]
    report.check("resource_limit_changed_actual_action", resource_effect)
    report.check("capability_difference_changed_actual_action", capability_effect)


def _verify_dag(report, plan, calls):
    steps = plan["steps"]
    required_products={"change.tif","before_water.tif","after_water.tif","valid_common.tif",
                       "before_rgb.tif","after_rgb.tif","stats.json","changes.geojson","map.png"}
    report.check("DAG_delivery_includes_real_imagery_overlays",
                 required_products.issubset(set(plan.get("delivery",{}).get("required_files",[]))))
    report.check("DAG_not_preassigned", all("assigned_node" not in s and len(s["allowed_nodes"]) >= 2 for s in steps))
    running = [c for c in calls if c.get("status") == "running"]
    success = {c["step_id"]:c for c in calls if c.get("status") == "succeeded"}
    report.check("actual_executed_DAG_complete", len(running) == len(steps) and {c["step_id"] for c in running} == {s["step_id"] for s in steps})
    coverage = np.zeros((plan["scientific_constraints"]["height"],plan["scientific_constraints"]["width"]),np.int32)
    for s in steps:
        rows = [r for r in running if r["step_id"] == s["step_id"]]
        if len(rows) != 1:continue
        r = rows[0]
        report.check("actual_plan_tool_and_params:"+s["step_id"], r["tool_id"] == s["tool_id"] and r["params"] == s["params"] and r["plan_id"] == plan["plan_id"])
        report.check("dependencies_before_start:"+s["step_id"], all(dep in success and success[dep]["timestamp"] < r["timestamp"] for dep in s["dependencies"]))
        if s["tool_id"] == "water_tile":
            row,col,h,w = s["params"]["window"]
            coverage[row:row+h,col:col+w] += 1
    report.check("tile_coverage_exact_once", bool(np.all(coverage == 1)))


def _verify_ground_outage(report, events, calls, controls):
    closures = [e for e in events if e.get("event") == "ground_link_closed"]
    report.check("ground_outage_observed", len(closures) == 1 and any(e.get("event") == "ground_link_opened" for e in events))
    if len(closures) != 1:return
    a,b = closures[0]["start_s"], closures[0]["end_s"]
    ready = [e for e in events if e.get("event") == "step_ready" and a < e["timestamp"] < b and e.get("dependencies")]
    passing = []
    for e in ready:
        started = [c for c in calls if c.get("status") == "running" and c["step_id"] == e["step_id"] and e["timestamp"] <= c["timestamp"] < b and c["node_id"] != "G"]
        ended = [c for c in calls if c.get("status") == "succeeded" and c["step_id"] == e["step_id"] and c["timestamp"] < b]
        if started and ended:passing.append(e["step_id"])
    report.check("new_dependency_ready_step_runs_during_ground_outage", bool(passing), passing)
    plans = [c for c in controls if c.get("event") == "delivered" and c.get("message_type") == "plan"]
    report.check("plan_received_before_ground_disconnect", len(plans) == 1 and plans[0]["timestamp"] <= a)
    delivered = [e for e in events if e.get("event") == "delivered"]
    report.check("delivery_after_ground_recovery", bool(delivered) and delivered[0]["timestamp"] >= b)


def _verify_transfer_outage(report, events, transfers, calls):
    closures = [e for e in events if e.get("event") == "data_link_closed"]
    report.check("actual_partial_transfer_interrupted", len(closures) == 1)
    if len(closures) != 1:return
    close = closures[0];tid = close["transfer_id"]
    rows = [r for r in transfers if r.get("transfer_id") == tid and r.get("event") not in ("capacity_budget", "capacity_charge")]
    start = next(r for r in rows if r["event"] == "started")
    pauses = [r for r in rows if r["event"] == "paused"]
    resumes = [r for r in rows if r["event"] == "resumed"]
    report.check("nonzero_incomplete_offset_at_outage", 0 < close["offset"] < start["size_bytes"])
    report.check("pause_resume_retained_offset", len(pauses) == 1 and len(resumes) == 1 and pauses[0]["offset"] == close["offset"] == resumes[0]["resume_offset"] == resumes[0]["retained_bytes"] and resumes[0]["retransmitted_bytes"] == 0 and resumes[0]["timestamp"] >= close["end_s"])
    report.check("incomplete_file_uncomputable", bool(pauses) and pauses[0].get("partial_computable") is False)
    after = [r for r in rows if r["event"] == "chunk" and r["timestamp"] >= close["end_s"]]
    report.check("resume_first_chunk_offset", bool(after) and after[0]["offset"] == close["offset"])
    verified = [r for r in rows if r["event"] == "verified"]
    consumers = [c for c in calls if c.get("status") == "running" and c["node_id"] == start["destination"] and start["artifact_id"] in c["input_artifact_ids"]]
    report.check("no_early_consume_of_paused_file", bool(verified) and bool(consumers) and all(c["timestamp"] > verified[0]["timestamp"] for c in consumers))


def _product_dir(run):
    return Path(_json(Path(run)/"runtime_result.json")["outputs_dir"])


def _nested_verdict(report, name, verdict):
    report.check(name, verdict.get("status") == "PASS", verdict)


def verify_suite(suite_dir):
    """Check the seven requested experiment relationships, not self-reported PASS."""
    suite = Path(suite_dir)
    report = _Report("E0-E6_independent_acceptance", suite_dir=str(suite))
    try:
        index = _json(suite/"suite_manifest.json")
        metadata = _json(suite/"run_manifest.json")
        experiments = index["experiments"]
        report.check("all_seven_experiments_present", set(experiments) == {"E0","E1","E2","E3","E4","E5","E6"})
        if set(experiments) != {"E0","E1","E2","E3","E4","E5","E6"}:
            return report.finish()
        data_root = Path(metadata["configuration"]["data_root"])
        baseline = Path(experiments["E0"])
        _nested_verdict(report, "E0_harness_material_evidence", verify_run(baseline))
        cases = sorted((data_root/"cases").iterdir())
        cases = [p for p in cases if (p/"task.json").is_file()]
        report.check("three_real_AOIs", len(cases) == 3)
        for case in [data_root/"earthbench/question2",data_root/"earthbench/question4",*cases]:
            _nested_verdict(report, "E0_independent_science:"+case.name, verify_science(case,baseline/"outputs"/case.name))
        normal = [Path(p) for p in experiments["E1"]]
        report.check("E1_three_AOI_runs", len(normal) == 3 and {_json(p/"plan.json")["case_id"] for p in normal} == {c.name for c in cases})
        for path in normal:
            _nested_verdict(report, "E1_runtime:"+path.name, verify_run(path))
            case_id = _json(path/"plan.json")["case_id"]
            _nested_verdict(report,"E1_equals_E0:"+case_id, compare_products(baseline/"outputs"/case_id,_product_dir(path)))
            report.check("E1_actual_analytical_reference:"+case_id, _json(path/"forecast.json")["source_kind"] == "analytical_reference")
        _verify_acquisition(report, data_root)
        _verify_feedback(report,index.get("feedback_records",[]))
        whole,tiled = [Path(p) for p in experiments["E2"]]
        f1,f2 = (_json(p/"forecast.json") for p in (whole,tiled))
        p1,p2 = (_json(p/"plan.json") for p in (whole,tiled))
        report.check("E2_same_observed_state", f1["observed_snapshot"] == f2["observed_snapshot"])
        report.check("E2_explicit_fixture", f1["source_kind"] == f2["source_kind"] == "test_fixture")
        allowed_change = {"forecast_id","fixture_name","fixture_description","node_forecasts","edge_forecasts"}
        report.check("E2_future_only_intervention", {k:v for k,v in f1.items() if k not in allowed_change} == {k:v for k,v in f2.items() if k not in allowed_change}
                     and (f1["node_forecasts"] != f2["node_forecasts"] or f1["edge_forecasts"] != f2["edge_forecasts"]))
        report.check("E2_same_science_and_input", p1["scientific_constraints"] == p2["scientific_constraints"] and _json(whole/"run_manifest.json")["inputs"] == _json(tiled/"run_manifest.json")["inputs"])
        report.check("E2_changed_real_DAG", p1["tile_windows"] != p2["tile_windows"] and len(p1["steps"]) != len(p2["steps"]))
        report.check("E2_forecast_consumed", p1["forecast_id"] == f1["forecast_id"] and p2["forecast_id"] == f2["forecast_id"])
        for label,path in (("whole",whole),("tiled",tiled)):
            _nested_verdict(report,"E2_actual_execution:"+label,verify_run(path))
        _nested_verdict(report,"E2_equivalent_products",compare_products(_product_dir(whole),_product_dir(tiled)))
        ordinary,busy = [Path(p) for p in experiments["E3"]]
        report.check("E3_identical_plan", _json(ordinary/"plan.json") == _json(busy/"plan.json"))
        report.check("E3_identical_forecast", _json(ordinary/"forecast.json") == _json(busy/"forecast.json"))
        report.check("E3_identical_inputs", _json(ordinary/"run_manifest.json")["inputs"] == _json(busy/"run_manifest.json")["inputs"])
        baseline_nodes = {c["step_id"]:c["node_id"] for c in _jsonl(ordinary/"tool_calls.jsonl") if c.get("status") == "running"}
        busy_nodes = {c["step_id"]:c["node_id"] for c in _jsonl(busy/"tool_calls.jsonl") if c.get("status") == "running"}
        report.check("E3_actual_execution_node_changed", baseline_nodes.keys() == busy_nodes.keys() and any(baseline_nodes[k] != busy_nodes[k] for k in baseline_nodes), {"baseline":baseline_nodes,"perturbed":busy_nodes})
        local_changes = [e for e in _jsonl(busy/"node_events.jsonl") if e.get("event") == "local_environment_observed"]
        report.check("E3_runtime_perturbation_observed_locally", bool(local_changes))
        _nested_verdict(report,"E3_runtime",verify_run(busy))
        _nested_verdict(report,"E3_equivalent_products",compare_products(_product_dir(ordinary),_product_dir(busy)))
        for label in ("E4","E5"):
            path = Path(experiments[label])
            _nested_verdict(report,label+"_runtime",verify_run(path))
            expected_scenario = "ground_outage" if label == "E4" else "transfer_outage"
            report.check(label+"_scenario",_json(path/"runtime_result.json")["scenario"] == expected_scenario)
            _nested_verdict(report,label+"_equivalent_products",compare_products(_product_dir(whole),_product_dir(path)))
        rerun = Path(experiments["E6"])
        command = _json(suite/"E6_command.json")
        report.check("E6_recorded_successful_command", command.get("exit_code") == 0 and command.get("clean_root_absent_before") is True and bool(command.get("argv")))
        report.check("E6_new_run_and_root", rerun.name != whole.name and rerun.resolve().is_relative_to((suite/"independent-rerun").resolve()))
        report.check("E6_same_plan_input_code", _json(rerun/"plan.json") == p1 and _json(rerun/"run_manifest.json")["inputs"] == _json(whole/"run_manifest.json")["inputs"]
                     and _json(rerun/"run_manifest.json")["code"]["source_sha256"] == _json(whole/"run_manifest.json")["code"]["source_sha256"])
        from datetime import datetime
        begun = datetime.fromisoformat(_json(rerun/"run_manifest.json")["started_at"]).timestamp()
        product_files = list(_product_dir(rerun).rglob("*"))
        report.check("E6_new_materialized_products", bool(product_files) and all(p.stat().st_mtime >= begun-1 for p in product_files if p.is_file()))
        old_jobs = {c["job_id"] for c in _jsonl(whole/"tool_calls.jsonl")}
        new_jobs = {c["job_id"] for c in _jsonl(rerun/"tool_calls.jsonl")}
        report.check("E6_distinct_execution_trace", bool(new_jobs) and old_jobs.isdisjoint(new_jobs))
        _nested_verdict(report,"E6_runtime",verify_run(rerun))
        _nested_verdict(report,"E6_equivalent_products",compare_products(_product_dir(whole),_product_dir(rerun)))
        _verify_source_boundaries(report)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        report.check("suite_verification_completed",False,f"{type(exc).__name__}: {exc}")
    return report.finish()


def _verify_feedback(report, records):
    report.check("actual_feedback_records_exist",bool(records))
    for record in records:
        previous,following = Path(record["preceding_run"]),Path(record["consuming_run"])
        result = _json(previous/"runtime_result.json")
        observation = record["observation"]
        forecast = _json(following/"forecast.json")
        plan = _json(following/"plan.json")
        report.check("feedback_actual_worker_observation", observation == result["observed_snapshot"] and observation.get("source_kind") == "actual_worker_observations" and result["status"] == "delivered")
        end = max(e["timestamp"] for e in _jsonl(previous/"tool_calls.jsonl") if e.get("status") == "succeeded")
        report.check("feedback_after_real_tool_completion", observation["timestamp"] >= end)
        # Explicit reversible epoch rebasing is the only allowed mutation of
        # measured feedback. Every non-time resource/queue value stays exact.
        rebased = json.loads(json.dumps(observation))
        cutoff = float(rebased["timestamp"])
        rebased["source_observation_timestamp"] = cutoff
        rebased["source_observation_clock"] = rebased.get("clock", "prior_run_monotonic")
        rebased["clock"] = "next_run_monotonic_epoch"
        rebased["timestamp"] = 0.0
        rebased["clock_rebase"] = "Prior completed run observation imported at next-run initialization; measurements unchanged"
        for node in rebased["nodes"]:
            node["source_observation_timestamp"] = node.get("timestamp", cutoff)
            node["timestamp"] = float(node["source_observation_timestamp"]) - cutoff
            node["now"] = 0.0
            if "busy_until_s" in node:
                node["busy_until_s"] = max(0.0, float(node["busy_until_s"])-cutoff)
        report.check("feedback_exact_epoch_rebase", forecast["observed_snapshot"] == rebased)
        report.check("feedback_new_forecast_consumed", forecast["forecast_id"] == record["forecast_id"] == plan["forecast_id"]
                     and forecast["forecast_id"] != _json(previous/"forecast.json")["forecast_id"])


def _verify_source_boundaries(report):
    import ast
    source = Path(__file__).parent
    forbidden = {"runtime","worker_node","science","data"}
    for name in ("forecast","policy"):
        tree = ast.parse((source/(name+".py")).read_text())
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node,ast.ImportFrom): imports.add((node.module or "").split(".")[-1])
            if isinstance(node,ast.Import): imports.update(alias.name.split(".")[-1] for alias in node.names)
        report.check("source_boundary:"+name,not (imports & forbidden),{"imported_modules":sorted(imports),"sha256":_sha(source/(name+".py"))})
    tree = ast.parse((source/"runtime.py").read_text())
    names = {n.func.id for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)}
    report.check("broker_does_not_execute_policy_or_science", not ({"choose_action","run_operator"}&names))
    verifier = ast.parse(Path(__file__).read_text())
    imports = [n.module for n in ast.walk(verifier) if isinstance(n,ast.ImportFrom)]
    report.check("verifier_independent_from_authors",not any(v and v.split(".")[-1] in forbidden|{"planning","statistics"} for v in imports))


def _verify_capacity_charges(report, transfers, controls, closures, config):
    budgets = {(r["source"],r["destination"],r["tick_id"]):r for r in transfers if r.get("event") == "capacity_budget"}
    charges = [r for r in transfers if r.get("event") == "capacity_charge"]
    report.check("actual_shared_capacity_charge_ledger", bool(budgets) and bool(charges))
    rates = {(e["source"],e["destination"]): float(e.get("bandwidth_bytes_s", e.get("throughput_bytes_s"))) for e in config["known_contact_plan"]}
    spent = defaultdict(list)
    packet_charges = defaultdict(int)
    for r in charges:
        spent[(r["source"],r["destination"],r["tick_id"])].append(r)
        packet_charges[(r["packet_kind"],r.get("message_id") or r.get("transfer_id"))] += r["charged_bytes"]
    report.check("all_charges_have_shared_budget", set(spent) <= set(budgets))
    arithmetic_errors=[];closed_errors=[];rate_errors=[];credit_errors=[]
    previous_budget = {}
    for key,budget in budgets.items():
        edge = key[:2];rate=rates[edge];cap=2*config["chunk_bytes"]
        before=float(budget["bucket_before_refill"])
        prior_time, prior_bucket = previous_budget.get(edge, (0.0, 0.0))
        max_credit = min(cap, prior_bucket + max(0.0, budget["interval_start"]-prior_time)*rate)
        if before > max_credit + 1e-6:
            credit_errors.append(list(key))
        expected = min(cap,before+budget["elapsed_s"]*rate) if budget["edge_up"] else 0.0
        if abs(budget["bucket_after_refill"]-expected)>1e-6 or before < 0 or before>cap+1e-6 or abs(budget["interval_end"]-budget["interval_start"]-budget["elapsed_s"])>1e-6:
            arithmetic_errors.append(list(key))
        if budget["edge_rate_bytes_s"] != rate or budget["burst_cap_bytes"] != cap:
            rate_errors.append(list(key))
        bucket=float(budget["bucket_after_refill"])
        for charge in spent.get(key,[]):
            amount=charge["charged_bytes"]
            if amount<=0 or amount>int(bucket) or abs(charge["bucket_before"]-bucket)>1e-6 or abs(charge["bucket_after"]-(bucket-amount))>1e-6:
                arithmetic_errors.append(list(key))
            bucket-=amount
            if not budget["edge_up"] or _edge_closed(closures,edge[0],edge[1],charge["timestamp"]):
                closed_errors.append(list(key))
        previous_budget[edge] = (budget["interval_end"], bucket)
    report.check("capacity_credit_carries_across_ticks", not credit_errors, {"invalid_ticks":credit_errors[:10]})
    report.check("shared_capacity_budget_arithmetic",not arithmetic_errors,{"budget_count":len(budgets),"charge_count":len(charges),"invalid_ticks":arithmetic_errors[:10]})
    report.check("capacity_configured_rates",not rate_errors,{"invalid_ticks":rate_errors[:10]})
    report.check("zero_capacity_charges_during_outages",not closed_errors,{"invalid_ticks":closed_errors[:10]})
    for start in [r for r in transfers if r.get("event") == "started"]:
        charged=packet_charges[("data",start["transfer_id"])]
        report.check("actual_payload_capacity_accounting:"+start["transfer_id"],charged == start["size_bytes"],{"charged_bytes":charged,"actual_archive_bytes":start["size_bytes"]})
    delivered_control = defaultdict(int)
    for row in controls:
        if row.get("event") != "delivered":continue
        kind = row["message_type"] if row["message_type"] in ("transfer_begin", "transport_ack") else "control"
        key = row.get("message_id") or row.get("transfer_id")
        delivered_control[(kind,key)] += row["size_bytes"]
    for key, count in delivered_control.items():
        report.check("actual_control_capacity_accounting:"+str(key), packet_charges[key] == count,
                     {"charged_bytes":packet_charges[key], "delivered_bytes":count})


def _verify_acquisition(report, data_root):
    mapping={"B02":"blue","B03":"green","B04":"red","B08":"nir","B11":"swir16","SCL":"scl"}
    manifest=_json(data_root/"acquisition_manifest.json")
    receipts=_jsonl(data_root/"acquisition.jsonl")
    report.check("acquisition_actual_download_or_cache_receipts", any(e.get("event") in ("download","cached_file_validated") for e in receipts))
    ranges=[e for e in receipts if e.get("event") == "http_range"]
    report.check("bounded_COG_range_receipts", bool(ranges) and all(e.get("http_status") == 206 and len(e.get("response_sha256", "")) == 64 and e["network_body_bytes"] == e["range_end"]-e["range_start"]+1 for e in ranges))
    for case in sorted((data_root/"cases").iterdir()):
        if not (case/"assets.json").is_file():continue
        for asset in _assets(case):
            item=_json(data_root/"catalog"/(case.name+"_"+asset["phase"]+"_item.json"))
            source=item["assets"][mapping[asset["band"]]]
            report.check("asset_matches_pinned_STAC:"+case.name+":"+asset["phase"]+":"+asset["band"],
                         item["id"] == asset["item_id"] and source["href"] == asset["source"] and source.get("raster:bands",[{}])[0] == asset["source_raster_band"])
            report.check("observation_time_matches_pinned_STAC:"+case.name+":"+asset["phase"]+":"+asset["band"],
                         asset.get("observation_time") == item["properties"]["datetime"]
                         and asset.get("observation_time_precision") == "instant")
            crop_receipts = [e for e in receipts if e.get("event") == "crop_complete"
                             and e.get("source") == asset["source"] and e.get("sha256") == asset["sha256"]
                             and e.get("phase") == asset["phase"] and e.get("band") == asset["band"]]
            report.check("acquisition_time_matches_crop_receipt:"+case.name+":"+asset["phase"]+":"+asset["band"],
                         any(e.get("acquired_at") == asset.get("acquired_at") for e in crop_receipts))
            if asset["band"] != "SCL":
                report.check("harmonization_flag_matches_original_item:"+case.name+":"+asset["phase"]+":"+asset["band"],
                             asset["earthsearch_boa_offset_applied"] == item["properties"].get("earthsearch:boa_offset_applied")
                             and asset["processing_baseline"] == item["properties"].get("s2:processing_baseline"))
            report.check("source_range_actually_requested:"+asset["path"], any(r["url"] == asset["source"] for r in ranges))
    for q in ("question2","question4"):
        catalog=_json(data_root/"catalog"/(q+"_hf_tree.json"))
        entries={Path(v["path"]).name:v for v in catalog if v.get("type") == "file"}
        for asset in _assets(data_root/"earthbench"/q):
            remote=entries[Path(asset["path"]).name]
            downloads = [e for e in receipts if e.get("event") == "download"
                         and e.get("url") == asset["source"] and e.get("sha256") == asset["sha256"]
                         and e.get("http_status") == 200]
            first_download = min((e["timestamp"] for e in downloads), default=None)
            report.check("EarthBench_actual_acquisition_timestamp:"+q+":"+asset["band"],
                         first_download is not None and asset.get("acquired_at") == first_download,
                         {"recorded": asset.get("acquired_at"), "first_successful_download": first_download})
            report.check("EarthBench_observation_date_precision:"+q+":"+asset["band"],
                         asset.get("observation_time_precision") == "date"
                         and asset.get("observation_time") == asset.get("observation_date"))
            if "lfs" in remote:
                matches = asset["sha256"] == remote["lfs"]["oid"]
                algorithm = "LFS_SHA256"
            else:
                data = (data_root/"earthbench"/q/asset["path"]).read_bytes()
                matches = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() == remote["oid"]
                algorithm = "Git_blob_SHA1_plus_independent_file_SHA256"
            report.check("EarthBench_pinned_source_hash:"+q+":"+asset["band"], matches and asset["bytes"] == remote["size"], {"authority":algorithm})


def _verify_worker_capacity(report, calls, config, plan):
    nodes={n["node_id"]:n for n in config["nodes"]}
    steps={s["step_id"]:s for s in plan["steps"]}
    pending_states={"accepted","queued","waiting_link","running"}
    for node_id,node in nodes.items():
        jobs={}
        charged=set()
        remaining_energy=float(node["energy_j"])
        for row in sorted((r for r in calls if r["node_id"]==node_id),key=lambda r:r["timestamp"]):
            job_id=row["job_id"]
            jobs[job_id]=row
            step=steps[row["step_id"]]
            if row["status"] in ("succeeded","failed") and job_id not in charged:
                remaining_energy-=float(step["estimated_energy_j"])
                charged.add(job_id)
            pending=[steps[r["step_id"]] for r in jobs.values() if r["status"] in pending_states]
            memory=sum(s["estimated_memory_bytes"] for s in pending)
            storage=sum(s["estimated_storage_bytes"] for s in pending)
            energy=sum(s["estimated_energy_j"] for s in pending)
            obs=row.get("resource_observation_after",{})
            label=node_id+":"+job_id+":"+row["status"]
            report.check("pending_work_reservations:"+label,
                         obs.get("memory_reserved_bytes")==memory
                         and obs.get("storage_reserved_bytes")==storage
                         and abs(obs.get("energy_reserved_j",math.inf)-energy)<=1e-9
                         and obs.get("pending_job_count")==len(pending))
            actual_used=obs.get("storage_used_bytes")
            bounds=(isinstance(actual_used,(int,float)) and 0<=actual_used<=node["storage_bytes"]
                    and actual_used+storage<=node["storage_bytes"]
                    and memory<=node["memory_bytes"] and energy<=remaining_energy+1e-9
                    and remaining_energy>=-1e-9)
            report.check("resource_capacity_not_oversubscribed:"+label,bounds)
            report.check("reserved_resource_availability:"+label,
                         bounds and obs.get("memory_bytes")==max(0,node["memory_bytes"]-memory)
                         and obs.get("storage_bytes")==max(0,node["storage_bytes"]-actual_used-storage)
                         and abs(obs.get("energy_j",math.inf)-max(0,remaining_energy-energy))<=1e-9
                         and abs(obs.get("energy_remaining_j",math.inf)-remaining_energy)<=1e-9)
            report.check("resource_snapshot_precedes_event:"+label,
                         obs.get("node_id")==node_id and obs.get("timestamp",math.inf)<=row["timestamp"])
        timeline=[]
        for row in calls:
            if row["node_id"] != node_id:continue
            if row.get("status") == "running":timeline.append((row["timestamp"],1,row))
            if row.get("status") in ("succeeded","failed"):timeline.append((row["timestamp"],-1,row))
        timeline.sort(key=lambda t:(t[0],t[1]))
        active=0;peak=0
        for _,delta,row in timeline:
            active+=delta;peak=max(peak,active)
            if delta<0 and "remaining_energy_j" in row:
                report.check("nonnegative_modeled_energy:"+node_id,row["remaining_energy_j"]>=0)
        report.check("actual_worker_concurrency:"+node_id,active == 0 and peak <= node["max_concurrency"],{"peak_simultaneous_tools":peak,"configured_limit":node["max_concurrency"]})
