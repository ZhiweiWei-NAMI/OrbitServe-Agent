"""Actual-pixel remote-sensing operators. No reference-answer or network access.

The public boundary is ``run_operator``. Asset scale/offset is applied once to
stored samples; the contract never guesses a universal reflectance multiplier.
"""
from __future__ import annotations

import hashlib
import json
import math
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from affine import Affine
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.features import shapes
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_geom
from rasterio.windows import Window

VERSION = "orbitserve-science-1.0.1"
FLOAT_NODATA = -9999.0
WATER_NODATA = 255
SCL_VALID = (4, 5, 6, 7)  # vegetation, bare soil, water, unclassified
CLASS_NAMES = {0: "unknown", 1: "stable_nonwater", 2: "stable_water", 3: "gained", 4: "lost"}
FORMULAS = {
    "NDVI": "(B08-B04)/(B08+B04)",
    "NDWI": "(B03-B08)/(B03+B08)",
    "MNDWI": "(B03-B11)/(B03+B11)",
    "water": "MNDWI > 0.0; threshold fixed for entire case and every tile",
    "diagnostics": "NDVI and NDWI are contextual outputs only; only MNDWI drives water classification",
    "change": "classified only on V=valid_before & valid_after",
}


def _json(path: Path) -> Any:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_asset(case_dir: Path, asset: dict) -> Path:
    relative = Path(asset["path"])
    if relative.is_absolute() or ".." in relative.parts or "evaluation" in relative.parts:
        raise ValueError(f"Asset must be a case-local input, got {relative}")
    candidate = (case_dir / relative).resolve()
    if not candidate.is_relative_to(case_dir.resolve()):
        raise ValueError("Asset symlink leaves case directory")
    if not candidate.is_file():
        raise FileNotFoundError(candidate)
    return candidate


def _metadata(case_dir: Path) -> tuple[dict, list[dict]]:
    task = _json(case_dir / "task.json")
    catalog = _json(case_dir / "assets.json")
    assets = catalog["assets"] if isinstance(catalog, dict) else catalog
    if not isinstance(assets, list) or not assets:
        raise ValueError("assets.json must contain a nonempty assets list")
    return task, assets


def _grid(task: dict) -> tuple[CRS, Affine, int, int]:
    crs = CRS.from_user_input(task["crs"])
    transform = Affine(*task["transform"][:6])
    height, width = int(task["height"]), int(task["width"])
    if min(height, width) <= 0 or abs(transform.determinant) <= 0:
        raise ValueError("Invalid target grid")
    return crs, transform, height, width


def _window(task: dict, raw: Any = None) -> tuple[int, int, int, int]:
    if raw is None:
        return 0, 0, int(task["height"]), int(task["width"])
    if len(raw) != 4 or any(float(x) != int(x) for x in raw):
        raise ValueError("window must be four integer row,col,height,width values")
    row, col, height, width = map(int, raw)
    if min(row, col) < 0 or min(height, width) <= 0 or row + height > task["height"] or col + width > task["width"]:
        raise ValueError("Tile window is outside the declared AOI")
    return row, col, height, width


def _asset(assets: list[dict], band: str, phase: str | None = None) -> dict:
    matches = [a for a in assets if str(a.get("band", "")).upper() == band and (phase is None or a.get("phase") == phase)]
    if len(matches) != 1:
        raise ValueError(f"Expected one {phase or ''} {band} asset, found {len(matches)}")
    return matches[0]


def _read_asset(case_dir: Path, asset: dict, task: dict, window: tuple[int, int, int, int]) -> tuple[np.ndarray, dict]:
    """Read a fixed full-grid VRT window, avoiding tile-specific warp boundaries."""
    path = _safe_asset(case_dir, asset)
    if "scale" not in asset or "offset" not in asset:
        raise ValueError(f"Missing explicit scale/offset for {asset['path']}")
    scale, offset = float(asset["scale"]), float(asset["offset"])
    if not math.isfinite(scale) or not math.isfinite(offset) or scale == 0:
        raise ValueError("Invalid physical scale/offset")
    is_category = str(asset.get("band", "")).upper() == "SCL"
    if is_category and (scale != 1 or offset != 0):
        raise ValueError("SCL must retain its category codes")
    if not is_category and asset.get("earthsearch_boa_offset_applied") is True and offset != 0:
        raise ValueError("Contradictory scaling: this COG already has the BOA offset applied; effective offset must be zero")
    crs, transform, full_height, full_width = _grid(task)
    row, col, height, width = window
    rio_window = Window(col, row, width, height)
    with rasterio.open(path) as source:
        source_nodata = asset.get("nodata", source.nodata)
        same_grid = source.crs == crs and source.transform == transform and source.width == full_width and source.height == full_height
        if same_grid:
            raw = source.read(1, window=rio_window, masked=True).astype(np.float64)
            valid = ~np.ma.getmaskarray(raw) & np.isfinite(raw.data)
            if source_nodata is not None:
                valid &= raw.data != float(source_nodata)
            arr = raw.data
        else:
            # The VRT spans the complete target grid for every tile. Reprojection
            # geometry and bilinear neighborhoods do not depend on tile extent.
            with WarpedVRT(source, crs=crs, transform=transform, height=full_height,
                           width=full_width, src_nodata=source_nodata, nodata=float("nan"),
                           dtype="float64", resampling=Resampling.nearest if is_category else Resampling.bilinear) as vrt:
                raw = vrt.read(1, window=rio_window, masked=True)
                arr = raw.data
                valid = ~np.ma.getmaskarray(raw) & np.isfinite(arr)
        result = np.full((height, width), np.nan, dtype=np.float64)
        result[valid] = arr[valid] * scale + offset
        provenance = {
            "path": asset["path"], "band": asset.get("band"), "phase": asset.get("phase"),
            "source_sha256": asset.get("sha256"), "source": asset.get("source"),
            "reported_scale": asset.get("reported_scale"), "reported_offset": asset.get("reported_offset"),
            "earthsearch_boa_offset_applied": asset.get("earthsearch_boa_offset_applied"),
            "processing_baseline": asset.get("processing_baseline"),
            "scaling_version": asset.get("scaling_version"),
            "scaling_authority_url": asset.get("scaling_authority_url"),
            "scaling_rationale": asset.get("scaling_rationale"),
            "item_id": asset.get("item_id"), "observation_time": asset.get("observation_time", asset.get("datetime")),
            "stored_valid_min": float(arr[valid].min()) if valid.any() else None,
            "stored_valid_max": float(arr[valid].max()) if valid.any() else None,
            "physical_valid_min": float(result[valid].min()) if valid.any() else None,
            "physical_valid_max": float(result[valid].max()) if valid.any() else None,
            "scale_applied_once": scale, "offset_applied_once": offset,
            "stored_dtype": source.dtypes[0], "stored_nodata": source_nodata,
            "source_raster_scales_not_reapplied": list(source.scales),
            "source_band_tags": source.tags(1), "same_grid": same_grid,
            "resampling": "none (already aligned)" if same_grid else ("nearest" if is_category else "bilinear"),
            "valid_samples_in_window": int(valid.sum()),
        }
    return result, provenance


def _ratio(numerator: np.ndarray, denominator: np.ndarray) -> np.ndarray:
    out = np.full(numerator.shape, np.nan, dtype=np.float64)
    valid = np.isfinite(numerator) & np.isfinite(denominator) & (denominator != 0)
    np.divide(numerator, denominator, out=out, where=valid)
    out[~np.isfinite(out)] = np.nan
    return out


def _write_raster(path: Path, array: np.ndarray, task: dict, window=None, nodata=None, tags=None) -> None:
    crs, transform, _, _ = _grid(task)
    row, col, height, width = _window(task, window)
    transform = transform * Affine.translation(col, row)
    arr = array
    if arr.shape != (height, width):
        raise ValueError(f"Raster shape {arr.shape} differs from window {(height, width)}")
    if np.issubdtype(arr.dtype, np.floating):
        arr = np.where(np.isfinite(arr), arr, FLOAT_NODATA).astype("float32")
        nodata = FLOAT_NODATA
    with rasterio.open(path, "w", driver="GTiff", width=width, height=height,
                       count=1, dtype=arr.dtype, crs=crs, transform=transform,
                       nodata=nodata, compress="deflate", predictor=3 if arr.dtype == np.float32 else 2) as dest:
        dest.write(arr, 1)
        dest.update_tags(implementation_version=VERSION, **(tags or {}))


def _describe(array: np.ndarray) -> dict:
    selected = array[np.isfinite(array)]
    if not selected.size:
        return {"valid_pixels": 0, "min": None, "max": None, "mean": None}
    return {"valid_pixels": int(selected.size), "min": float(selected.min()), "max": float(selected.max()), "mean": float(selected.mean())}


def _water_threshold(task: dict, params: dict) -> float:
    method = str(task.get("method", "MNDWI")).upper()
    threshold = float(params.get("threshold", task.get("threshold", 0.0)))
    if method != "MNDWI" or threshold != 0.0 or float(task.get("threshold", 0.0)) != 0.0:
        raise ValueError("V0 water method is case-wide MNDWI > 0.0; per-tile thresholds are forbidden")
    return threshold


def _water_tile(case_dir: Path, output_dir: Path, params: dict) -> dict:
    task, assets = _metadata(case_dir)
    threshold = _water_threshold(task, params)
    window = _window(task, params.get("window"))
    arrays, provenance = {}, []
    for phase in ("before", "after"):
        bands = {}
        for band in ("B02", "B03", "B04", "B08", "B11", "SCL"):
            bands[band], record = _read_asset(case_dir, _asset(assets, band, phase), task, window)
            provenance.append(record)
        b, g, r, n, s = (bands[k] for k in ("B02", "B03", "B04", "B08", "B11"))
        observed = np.isin(bands["SCL"], SCL_VALID)
        for band in (b, g, r, n, s):
            observed &= np.isfinite(band)
        indices = {"NDVI": _ratio(n-r, n+r), "NDWI": _ratio(g-n, g+n),
                   "MNDWI": _ratio(g-s, g+s)}
        valid = observed & np.isfinite(indices["MNDWI"])
        arrays[f"valid_{phase}"] = valid.astype("uint8")
        arrays[f"{phase}_water"] = np.where(valid, indices["MNDWI"] > threshold, WATER_NODATA).astype("uint8")
        for key, index in indices.items():
            arrays[f"{phase}_{key.lower()}"] = np.where(valid, index, np.nan)
    common = arrays["valid_before"].astype(bool) & arrays["valid_after"].astype(bool)
    before, after = arrays["before_water"] == 1, arrays["after_water"] == 1
    change = np.zeros(common.shape, dtype="uint8")
    change[common & ~before & ~after] = 1
    change[common & before & after] = 2
    change[common & ~before & after] = 3
    change[common & before & ~after] = 4
    arrays["valid_common"], arrays["change"] = common.astype("uint8"), change
    files = []
    for key, arr in arrays.items():
        name = f"{key}.tif"
        _write_raster(output_dir / name, arr, task, window,
                      nodata=WATER_NODATA if key.endswith("_water") else None,
                      tags={"formula": FORMULAS.get(key.split("_", 1)[-1].upper(), ""), "threshold": str(threshold)})
        files.append(name)
    metadata = {"implementation_version": VERSION, "case_id": task.get("case_id"),
                "window": list(window), "target_grid": {k: task[k] for k in ("crs", "transform", "height", "width")},
                "threshold": threshold, "method": "MNDWI", "files": files,
                "file_hashes": {name: _sha256(output_dir / name) for name in files},
                "input_scaling": provenance, "formulas": FORMULAS,
                "scl_valid_classes": list(SCL_VALID), "valid_common_pixels": int(common.sum()),
                "class_counts": {name: int((change == code).sum()) for code, name in CLASS_NAMES.items()}}
    _write_json(output_dir / "tile.json", metadata)
    return {"status": "succeeded", "files": files + ["tile.json"], "statistics": metadata,
            "formulas": FORMULAS, "window": list(window)}


def _pixel_area(task: dict) -> float:
    crs, transform, _, _ = _grid(task)
    if not crs.is_projected:
        raise ValueError("Water area requires a projected metric target grid")
    unit, factor = crs.linear_units_factor
    if not math.isfinite(factor) or factor <= 0:
        raise ValueError(f"Unsupported projected linear units: {unit}")
    return abs(transform.determinant) * factor * factor


def _water_statistics(arrays: dict, task: dict) -> dict:
    change = arrays["change"]
    common = arrays["valid_common"] == 1
    area = _pixel_area(task)
    count = {name: int((change == code).sum()) for code, name in CLASS_NAMES.items()}
    total, valid = int(change.size), int(common.sum())
    if not valid:
        raise ValueError("No common valid observation in this AOI; water change is not computable")
    if count["unknown"] + valid != total or sum(count[k] for k in CLASS_NAMES.values() if k != "unknown") != valid:
        raise ValueError("Change classes fail conservation")
    return {"case_id": task.get("case_id"), "method": "MNDWI", "threshold": 0.0,
            "total_pixels": total, "valid_pixels": valid, "valid_common_pixels": valid,
            "unknown_pixels": count["unknown"], "valid_coverage_fraction": valid / total,
            "pixel_area_m2": area, "area_method": "absolute affine determinant times projected linear-unit conversion squared; map-grid area",
            "class_counts": count, "class_area_m2": {key: value * area for key, value in count.items()},
            "aoi_area_m2": total * area, "common_valid_area_m2": valid * area, "unknown_area_m2": count["unknown"] * area,
            "before_valid_pixels": int(arrays["valid_before"].sum()), "after_valid_pixels": int(arrays["valid_after"].sum()),
            "before_valid_coverage_fraction": float(arrays["valid_before"].mean()),
            "after_valid_coverage_fraction": float(arrays["valid_after"].mean()),
            "before_water_common_pixels": int(((arrays["before_water"] == 1) & common).sum()),
            "after_water_common_pixels": int(((arrays["after_water"] == 1) & common).sum()),
            "before_water_observed_pixels": int((arrays["before_water"] == 1).sum()),
            "after_water_observed_pixels": int((arrays["after_water"] == 1).sum()),
            "gained_pixels": count["gained"], "lost_pixels": count["lost"],
            "gained_area_m2": count["gained"] * area, "lost_area_m2": count["lost"] * area,
            "net_change_area_m2": (count["gained"] - count["lost"]) * area,
            "formulas": FORMULAS, "scl_valid_classes": list(SCL_VALID),
            "class_encoding": {str(k): v for k, v in CLASS_NAMES.items()},
            "water_encoding": {"0": "observed_nonwater", "1": "observed_water", "255": "phase_invalid"},
            "limitations": ["Threshold-derived water-change candidates; no flood labels or accuracy assessment.",
                            "SCL class 7 is retained; unclassified observations can contain residual contamination.",
                            "No geometric registration error estimate; shared projected grid does not establish subpixel accuracy."]}


def _ring_area(ring: list) -> float:
    points = np.asarray(ring, dtype=np.float64)
    # Translate to reduce cancellation at UTM coordinate magnitudes.
    points = points - points[0]
    return abs(float(np.dot(points[:, 0], np.roll(points[:, 1], -1)) - np.dot(points[:, 1], np.roll(points[:, 0], -1)))) / 2


def _vectorize(change: np.ndarray, task: dict, destination: Path) -> dict:
    crs, transform, _, _ = _grid(task)
    pixel_area = _pixel_area(task)
    features = []
    for geometry, value in shapes(change, mask=np.isin(change, (3, 4)), transform=transform, connectivity=4):
        rings = geometry["coordinates"]
        grid_area = _ring_area(rings[0]) - sum(_ring_area(ring) for ring in rings[1:])
        pixels = int(round(grid_area / abs(transform.determinant)))
        features.append({"type": "Feature", "geometry": transform_geom(crs, "EPSG:4326", geometry, precision=9),
                         "properties": {"class_code": int(value), "change": CLASS_NAMES[int(value)],
                                        "pixel_count": pixels, "area_m2": pixels * pixel_area,
                                        "area_basis": "source projected raster pixels"}})
    collection = {"type": "FeatureCollection", "features": features,
                  "description": "MNDWI candidate changes; RFC 7946 longitude/latitude geometries, projected-grid area attributes."}
    _write_json(destination, collection)
    return {"features": len(features), "geometry_crs": "EPSG:4326", "connectivity": 4}


def _draw_map(arrays: dict, task: dict, path: Path, *, continuous: bool = False) -> None:
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.load_default(size=16)
    title_font = ImageFont.load_default(size=20)
    if continuous:
        values = arrays["value"]
        valid = np.isfinite(values)
        unit = np.clip(values, 0, 1)
        unit = np.nan_to_num(unit)
        rgb = np.stack([255 * unit, 220 * (1 - abs(unit - .5) * 2), 255 * (1 - unit)], axis=-1).astype("uint8")
        rgb[~valid] = (165, 165, 165)
        panel = Image.fromarray(rgb)
        panel.thumbnail((900, 430), Image.Resampling.NEAREST)
        canvas = Image.new("RGB", (940, 520), "white")
        canvas.paste(panel, (20 + (900 - panel.width) // 2, 50 + (430 - panel.height) // 2))
        draw = ImageDraw.Draw(canvas)
        draw.text((20, 10), str(task.get("case_id", "Earth-Bench")) + " - " + arrays.get("label", "Index"), font=title_font, fill="black")
        draw.text((20, 490), "Blue: 0 | Green: 0.5 | Red: 1 | Gray: invalid. Display clipped to [0,1].", font=font, fill="black")
    else:
        palette = np.array([(165, 165, 165), (232, 224, 206), (35, 108, 177), (20, 169, 145), (218, 89, 56)], dtype="uint8")
        canvas = Image.new("RGB", (1000, 465), "white")
        draw = ImageDraw.Draw(canvas)
        draw.text((20, 10), f"{task.get('case_id', 'Water change')}: MNDWI > 0.0 candidates", font=title_font, fill="black")
        for i, phase in enumerate(("before", "after", "change")):
            if phase == "change":
                labels = arrays["change"]
            else:
                water = arrays[f"{phase}_water"]
                labels = np.where(water == WATER_NODATA, 0, water + 1)
            rgb = palette[labels]
            panel = Image.fromarray(rgb).resize((300, 300), Image.Resampling.NEAREST)
            x = 20 + i * 330
            draw.text((x, 45), phase.capitalize(), font=font, fill="black")
            canvas.paste(panel, (x, 70))
        for i, (code, name) in enumerate(CLASS_NAMES.items()):
            x = 20 + i * 195
            draw.rectangle((x, 395, x + 18, 413), fill=tuple(palette[code]))
            draw.text((x + 24, 394), name, font=font, fill="black")
        draw.text((20, 435), "Common-valid comparison only. Grid-aligned quicklook; GeoTIFFs retain full resolution and georeferencing.", font=font, fill="black")
    canvas.save(path, format="PNG")


def _water_merge(case_dir: Path, output_dir: Path, params: dict) -> dict:
    # Intentionally do not open assets.json or any original imagery here.
    task = _json(case_dir / "task.json")
    _water_threshold(task, params)
    crs, transform, height, width = _grid(task)
    tile_dirs = [Path(value) for value in params.get("tile_dirs", [])]
    if not tile_dirs:
        raise ValueError("water_merge requires received tile_dirs")
    coverage = np.zeros((height, width), dtype="uint8")
    arrays: dict[str, np.ndarray] = {}
    expected_keys = {"before_water", "after_water", "valid_before", "valid_after", "valid_common", "change"}
    expected_keys |= {f"{phase}_{index.lower()}" for phase in ("before", "after") for index in ("NDVI", "NDWI", "MNDWI")}
    tile_records = []
    for directory in tile_dirs:
        meta = _json(directory / "tile.json")
        if meta.get("case_id") != task.get("case_id") or meta.get("threshold") != 0.0 or meta.get("method") != "MNDWI":
            raise ValueError("Incompatible tile case, threshold, or method")
        if meta.get("implementation_version") != VERSION:
            raise ValueError("Mixed science implementation versions")
        row, col, h, w = _window(task, meta["window"])
        target = np.s_[row:row+h, col:col+w]
        if np.any(coverage[target]):
            raise ValueError("Overlapping or duplicated tile windows")
        if set(name[:-4] for name in meta["files"] if name.endswith(".tif")) != expected_keys:
            raise ValueError("Incomplete/unexpected tile raster product set")
        for key in sorted(expected_keys):
            name = f"{key}.tif"
            if _sha256(directory / name) != meta["file_hashes"].get(name):
                raise ValueError(f"Tile artifact hash mismatch: {name}")
            with rasterio.open(directory / name) as src:
                if src.crs != crs or src.transform != transform * Affine.translation(col, row) or src.shape != (h, w):
                    raise ValueError("Tile raster grid does not match its declared window")
                data = src.read(1)
                if np.issubdtype(data.dtype, np.floating):
                    data = np.where(data == src.nodata, np.nan, data)
                if key not in arrays:
                    arrays[key] = np.empty((height, width), dtype=data.dtype)
                arrays[key][target] = data
        coverage[target] = 1
        tile_records.append({"window": meta["window"], "input_scaling": meta.get("input_scaling", []), "file_hashes": meta["file_hashes"]})
    if not np.all(coverage == 1):
        raise ValueError(f"Tiles leave {int((coverage == 0).sum())} pixels uncovered")
    common = (arrays["valid_before"] == 1) & (arrays["valid_after"] == 1)
    before, after = arrays["before_water"], arrays["after_water"]
    expected_change = np.zeros((height, width), dtype="uint8")
    expected_change[common & (before == 0) & (after == 0)] = 1
    expected_change[common & (before == 1) & (after == 1)] = 2
    expected_change[common & (before == 0) & (after == 1)] = 3
    expected_change[common & (before == 1) & (after == 0)] = 4
    if not np.array_equal(common, arrays["valid_common"] == 1) or not np.array_equal(expected_change, arrays["change"]):
        raise ValueError("Tile water/valid/change products disagree")
    stats = _water_statistics(arrays, task)
    stats.update({"implementation_version": VERSION, "tile_count": len(tile_dirs), "tile_provenance": tile_records,
                  "grid": {key: task[key] for key in ("crs", "transform", "height", "width")},
                  "indices": {key: _describe(arr) for key, arr in arrays.items() if np.issubdtype(arr.dtype, np.floating)},
                  "merge_read_scope": "task.json and received tile outputs only; original imagery not opened"})
    files = []
    for key in sorted(arrays):
        name = f"{key}.tif"
        _write_raster(output_dir / name, arrays[key], task, nodata=WATER_NODATA if key.endswith("_water") else None,
                      tags={"formula": FORMULAS.get(key.split("_", 1)[-1].upper(), ""), "threshold": "0.0"})
        files.append(name)
    stats["vectorization"] = _vectorize(arrays["change"], task, output_dir / "changes.geojson")
    _draw_map(arrays, task, output_dir / "map.png")
    _write_json(output_dir / "stats.json", stats)
    files += ["changes.geojson", "map.png", "stats.json"]
    return {"status": "succeeded", "files": files, "statistics": stats, "formulas": FORMULAS}


def _water_full(case_dir: Path, output_dir: Path, params: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix=".full-tile-", dir=output_dir) as temporary:
        _water_tile(case_dir, Path(temporary), {k: v for k, v in params.items() if k != "window"})
        return _water_merge(case_dir, output_dir, {"tile_dirs": [temporary], "threshold": params.get("threshold", 0.0)})


def _earthbench_ndvi(case_dir: Path, output_dir: Path, params: dict) -> dict:
    task, assets = _metadata(case_dir)
    window = _window(task)
    ndvi, provenance = _read_asset(case_dir, _asset(assets, "NDVI"), task, window)
    ndvi[(ndvi < -1) | (ndvi > 1)] = np.nan
    if not np.any(np.isfinite(ndvi)):
        raise ValueError("No physically valid supplied NDVI samples")
    _write_raster(output_dir / "ndvi.tif", ndvi, task, tags={"formula": "supplied NDVI DN * asset.scale + asset.offset"})
    stats = {"case_id": task.get("case_id"), "implementation_version": VERSION,
             "operation": "decode and summarize supplied NDVI product", "ndvi": _describe(ndvi),
             "input_scaling": [provenance], "limitations": ["No red/NIR input is supplied in these Earth-Bench cases; this is not a new spectral NDVI retrieval."]}
    _write_json(output_dir / "stats.json", stats)
    _draw_map({"value": ndvi, "label": "supplied NDVI"}, task, output_dir / "map.png", continuous=True)
    return {"status": "succeeded", "files": ["ndvi.tif", "stats.json", "map.png"], "statistics": stats,
            "formulas": {"decode": "physical NDVI = stored DN * asset.scale + asset.offset"}}


def _earthbench_tvdi(case_dir: Path, output_dir: Path, params: dict) -> dict:
    task, assets = _metadata(case_dir)
    window = _window(task)
    ndvi, ndvi_provenance = _read_asset(case_dir, _asset(assets, "NDVI"), task, window)
    lst, lst_provenance = _read_asset(case_dir, _asset(assets, "LST"), task, window)
    valid = np.isfinite(ndvi) & np.isfinite(lst) & (ndvi >= 0) & (ndvi <= 1) & (lst > 0)
    if int(valid.sum()) < 100:
        raise ValueError("TVDI needs at least 100 valid NDVI/LST pairs")
    x, y = ndvi[valid], lst[valid]
    n_bins = int(params.get("n_bins", 100))
    if n_bins < 2 or n_bins > 1000:
        raise ValueError("TVDI n_bins must be in [2,1000]")
    if x.max() == x.min():
        raise ValueError("TVDI edge fitting requires nonconstant NDVI")
    edges = np.linspace(x.min(), x.max(), n_bins + 1)
    # Include the rightmost value in the last bin. Upstream '<' upper bound
    # drops its maximum; this explicit correction does not tune any answer.
    bin_id = np.minimum(np.searchsorted(edges, x, side="right") - 1, n_bins - 1)
    minima = np.full(n_bins, np.inf)
    maxima = np.full(n_bins, -np.inf)
    np.minimum.at(minima, bin_id, y)
    np.maximum.at(maxima, bin_id, y)
    counts = np.bincount(bin_id, minlength=n_bins)
    populated = counts > 0
    centers = (edges[:-1] + edges[1:]) / 2
    if int(populated.sum()) < 2:
        raise ValueError("Too few populated NDVI bins for TVDI edge regression")
    dry_slope, dry_intercept = np.polyfit(centers[populated], maxima[populated], 1)
    wet_slope, wet_intercept = np.polyfit(centers[populated], minima[populated], 1)
    dry, wet = ndvi * dry_slope + dry_intercept, ndvi * wet_slope + wet_intercept
    denominator = dry - wet
    fit_valid = valid & np.isfinite(denominator) & (denominator > 0)
    if not np.any(fit_valid):
        raise ValueError("TVDI dry and wet fits provide no positive thermal span")
    raw_tvdi = _ratio(lst - wet, denominator)
    tvdi = np.where(fit_valid, np.clip(raw_tvdi, 0, 1), np.nan)
    threshold = float(params.get("threshold", task.get("threshold", 0.75)))
    if not 0 <= threshold <= 1:
        raise ValueError("TVDI threshold must be in [0,1]")
    exceeds = np.where(fit_valid, tvdi > threshold, WATER_NODATA).astype("uint8")
    total, selected = int(fit_valid.sum()), int((exceeds == 1).sum())
    formulas = {"scale": "physical = stored DN * asset.scale + asset.offset (once)",
                "dry_edge": "Tdry(NDVI)=dry_slope*NDVI+dry_intercept",
                "wet_edge": "Twet(NDVI)=wet_slope*NDVI+wet_intercept",
                "TVDI": "clip((LST-Twet(NDVI))/(Tdry(NDVI)-Twet(NDVI)),0,1)",
                "percentage": "100*count(TVDI > threshold)/count(valid TVDI)"}
    stats = {"case_id": task.get("case_id"), "implementation_version": VERSION,
             "original_requested_task": task.get("original_question", task.get("original_task")),
             "effective_task": task.get("effective_question", task.get("corrected_question", task.get("question", task.get("description", "Compute TVDI from supplied NDVI and LST; report valid-pixel percentage above 0.75.")))),
             "input_task_erratum": task.get("erratum"), "input_task_caveat": task.get("caveat"),
             "input_scaling": [ndvi_provenance, lst_provenance], "ndvi": _describe(ndvi), "lst_kelvin": _describe(lst),
             "tvdi": _describe(tvdi), "total_pixels": int(ndvi.size), "valid_input_pairs": int(valid.sum()),
             "valid_pixels": total, "unknown_pixels": int(ndvi.size) - total,
             "valid_coverage_fraction": total / int(ndvi.size), "threshold": threshold,
             "pixels_above_threshold": selected, "percentage_above_threshold": 100 * selected / total,
             "fraction_above_threshold": selected / total,
             "fit": {"method": "unweighted ordinary least squares of within-bin minima and maxima",
                     "n_bins": n_bins, "populated_bins": int(populated.sum()),
                     "ndvi_fit_min": float(x.min()), "ndvi_fit_max": float(x.max()),
                     "dry_slope": float(dry_slope), "dry_intercept": float(dry_intercept),
                     "wet_slope": float(wet_slope), "wet_intercept": float(wet_intercept),
                     "bin_edges": edges.tolist(), "bin_counts": counts.tolist(),
                     "bin_centers": centers[populated].tolist(), "lst_max_kelvin": maxima[populated].tolist(),
                     "lst_min_kelvin": minima[populated].tolist(),
                     "nonpositive_span_rejected_pixels": int((valid & ~fit_valid).sum()),
                     "raw_below_zero_pixels": int((fit_valid & (raw_tvdi < 0)).sum()),
                     "raw_above_one_pixels": int((fit_valid & (raw_tvdi > 1)).sum())},
             "formulas": formulas,
             "upstream_algorithm": "https://github.com/opendatalab/Earth-Agent/blob/main/agent/tools/Index.py#L1",
             "method_differences": ["Honor input nodata and metadata scale once.", "Include maximum NDVI in the final bin.",
                                    "Nonpositive fitted thermal span is invalid, not epsilon-replaced.", "Raise on insufficient data instead of returning a successful empty image."],
             "limitations": ["Input NDVI is a provided product, not recomputed from red/NIR.",
                             "MODIS NDVI (250 m) and LST (1 km) are supplied on an oversampled 30 m EPSG:3857 grid; no 30 m scientific resolution claim.",
                             "NDVI composite and daily LST do not have identical temporal support.",
                             "No supplied MODIS QA bands; valid/nodata and physical-range filtering only.",
                             "TVDI edge fit is relative to this supplied region; percentage is descriptive, not validated drought accuracy."]}
    if task.get("case_id") == "question2" or case_dir.name == "question2":
        stats["upstream_task_erratum"] = "Use actual Sichuan_2021-07-12_NDVI/LST files. Upstream EVI/year mismatches are not silently substituted."
    for name, arr in (("ndvi", ndvi), ("lst", lst), ("tvdi", tvdi), ("valid", fit_valid.astype("uint8")), ("above_threshold", exceeds)):
        _write_raster(output_dir / f"{name}.tif", arr, task, nodata=WATER_NODATA if name == "above_threshold" else None)
    _draw_map({"value": tvdi, "label": "TVDI"}, task, output_dir / "map.png", continuous=True)
    _write_json(output_dir / "stats.json", stats)
    return {"status": "succeeded", "files": ["ndvi.tif", "lst.tif", "tvdi.tif", "valid.tif", "above_threshold.tif", "map.png", "stats.json"],
            "statistics": stats, "formulas": formulas}


def run_operator(tool_id: str, case_dir: str | Path, output_dir: str | Path, params: dict) -> dict:
    """Compute actual products, raising errors instead of returning fake success."""
    operators = {"earthbench_ndvi": _earthbench_ndvi, "earthbench_tvdi": _earthbench_tvdi,
                 "water_tile": _water_tile, "water_merge": _water_merge, "water_full": _water_full}
    if tool_id not in operators:
        raise ValueError(f"Unknown scientific operator: {tool_id}")
    source, destination = Path(case_dir), Path(output_dir)
    if "evaluation" in source.parts:
        raise ValueError("Evaluation material is not a scientific input")
    destination.mkdir(parents=True, exist_ok=True)
    result = operators[tool_id](source, destination, dict(params or {}))
    result["tool_id"] = tool_id
    result["implementation_version"] = VERSION
    return result
