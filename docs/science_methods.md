# Scientific methods and reproducibility boundaries

Implementation: `src/orbitserve/science.py`, version `orbitserve-science-1.0.1`.
Public API: `run_operator(tool_id, case_dir, output_dir, params) -> dict`.
The result contains `status`, relative `files`, `statistics`, `formulas`, and the
implementation version. Missing data or invalid scientific prerequisites raise
an exception; the harness must record a failed call rather than success.

## Input contract and scaling

Only case-local `task.json`, `assets.json`, and declared input rasters are used.
There is no network client, reference-answer loader, or result lookup in the
science module. Absolute asset paths, parent-directory traversal, and symlinks
that escape the case directory are rejected. Evaluation directories are not
valid scientific inputs.

Every asset requires an explicit scale and offset. Physical values are computed
as `stored sample * asset.scale + asset.offset` exactly once, after masking the
stored nodata. Rasterio does not apply scales automatically when reading these
samples. The stored dtype, nodata, raster scale metadata, band tags, applied
scale/offset, and alignment operation are preserved in output provenance.
No universal division by 10000 is performed. In particular, an already-scaled
floating-point input must have metadata scale 1 and offset 0.

### Resolved Earth Search offset inconsistency

The selected Sentinel-2 scenes were reprocessed with baseline 05.00. Their
item-level `earthsearch:boa_offset_applied` flag is true, while legacy asset
`raster:bands.offset` reports -0.1. The provider confirms this legacy metadata
inconsistency and identifies the item flag as the applicable indicator:
[Earth Search maintainer discussion, issue 9](https://github.com/Element84/earth-search/issues/9).

Applying -0.1 again produced large negative water reflectances: for example,
the after-date Lake Mead SCL-water median raw DN is approximately 391 in green,
90 in NIR, and 48 in SWIR. The acquisition module therefore records effective
offset 0 for these explicitly harmonized COGs, while retaining reported offset
-0.1, the item flag, processing baseline, authority URL, and rationale. Raw
pixel arrays were not edited; metadata/tag/hash revisions are documented in the
acquisition audit. Scientific products preserve that resolution record.

The science reader fails closed if an asset says the BOA offset was already
applied yet requests a nonzero effective offset. It does not silently override
asset metadata. This correction is specific to the documented source state;
other Sentinel-2 sources must retain their own verified scale/offset semantics.

Water cases use the complete target grid from task metadata. Already-aligned
inputs are read by integer windows. Otherwise a VRT for the *complete* target
grid is built before reading its tile window. Continuous bands use bilinear
resampling and SCL uses nearest neighbor. A tile-specific target extent is not
used to select reprojection neighborhoods. The original data acquisition
records retain the native-grid/resampling history for prealigned inputs.

## Earth-Bench question2 and question4

Both actual input pairs contain a supplied NDVI raster and an LST raster.
There are no red/NIR bands from which to independently retrieve NDVI. The
`earthbench_ndvi` operator therefore explicitly decodes and summarizes the
provided NDVI product. Both substantive benchmark workflows use
`earthbench_tvdi`.

Actual inspected input metadata:

| Case | Stored NDVI | Stored LST | Exported grid |
|---|---|---|---|
| question2 | Sichuan_2021-07-12, int16, scale 0.0001, nodata -32768 | uint16, scale 0.02 K, nodata 0 | EPSG:3857, 2598 x 863, 30 m |
| question4 | Chengdu_2022-08-13, int16, scale 0.0001, nodata -32768 | uint16, scale 0.02 K, nodata 0 | EPSG:3857, 2598 x 1726, 30 m |

The `.scales` field in these files is 1; the explicit band tags record the
actual MODIS scaling factors. The observed integer values and product names
confirm that the factors have not already been applied. Source resolution is
250 m for NDVI and 1 km for LST. A 30 m export does not create 30 m physical
information. The NDVI composite and daily LST also have different temporal
support. No MODIS QA bands or agricultural land-cover mask are supplied;
statistics refer to the available valid region, not a verified crop subset.

Question2 upstream evaluation prose contains year/EVI inconsistencies. The
derived executable task uses the actual 2021 NDVI/LST pair. It does not claim
an EVI calculation, a 2022 observation, or red/NIR NDVI retrieval. Reference
answers and reference tool traces are never provided to the operator.

### TVDI algorithm

The algorithm family was inspected in the upstream scientific implementation:
[Earth-Agent Index.py](https://github.com/opendatalab/Earth-Agent/blob/main/agent/tools/Index.py).
It is implemented locally rather than invoking an evaluation service.

1. Decode NDVI and LST using each input's explicit physical scale.
2. Retain finite, non-nodata pairs with `0 <= NDVI <= 1` and `LST > 0 K`.
3. Require at least 100 pairs and nonconstant NDVI.
4. Form 100 equal-width bins spanning the valid NDVI minimum and maximum.
   Include the maximum in the final bin. For each populated bin, calculate
   observed minimum and maximum LST, using its center as the x coordinate.
5. Independently fit two unweighted ordinary least-squares lines:
   `Tdry = dry_slope * NDVI + dry_intercept` and
   `Twet = wet_slope * NDVI + wet_intercept`.
6. Calculate `TVDI = clip((LST - Twet)/(Tdry - Twet), 0, 1)` where the fitted
   thermal span is finite and strictly positive. Other pixels remain unknown.
7. Calculate `100 * count(TVDI > 0.75) / count(valid TVDI)`.

The result records bin edges, population counts, observed extrema, fit
coefficients, fit range, rejected nonpositive spans, clipped-tail counts, and
numerator/denominator of the reported percentage. A caller may explicitly set
`n_bins` (2–1000) or the reporting threshold; default benchmark values are
100 and 0.75. These settings must be recorded by the harness.

Differences from the inspected upstream routine are deliberate, disclosed
scientific safeguards: input nodata is respected; scales are metadata-specific;
the largest NDVI is included in the final bin; nonpositive thermal spans are
invalid instead of replaced by epsilon; insufficient data raises an error.
No parameter is selected to match a reference answer.

Outputs are `ndvi.tif`, `lst.tif`, `tvdi.tif`, `valid.tif`,
`above_threshold.tif`, `map.png`, and `stats.json`. Continuous products are
float32 with -9999 nodata. `above_threshold.tif` has 0/1 values and 255 for
unknown. `valid.tif` contains literal 0/1 values without a nodata tag so invalid
coverage can be independently counted. LST is Kelvin. TVDI is a region-relative
proxy; no validated drought-accuracy claim is made.

## Water indices and masks

Inputs for both dates: blue B02, green B03, red B04, near infrared B08, shortwave
infrared B11, and SCL. All index formulas operate on scaled physical values:

| Product | Formula |
|---|---|
| NDVI | (B08 - B04) / (B08 + B04) |
| NDWI, green/NIR convention | (B03 - B08) / (B03 + B08) |
| MNDWI | (B03 - B11) / (B03 + B11) |

A zero or nonfinite denominator yields an unknown index, not a fabricated zero.
For each date, the observed region requires finite values for all five bands
and SCL in `{4,5,6,7}`: vegetation, bare soil, water, unclassified. SCL 0/1/2/3,
8/9/10, and 11 are excluded (nodata, defective, dark/shadow, cloud, cirrus,
snow/ice). Retaining SCL 7 is explicit and may leave residual contamination.
Phase validity additionally requires a finite MNDWI. Other indices have their
own denominator validity within this phase-valid mask.

Only MNDWI drives water classification. NDVI and green/NIR NDWI are contextual
diagnostics and do not change water masks. Water is `MNDWI > 0.0`. Equality is
nonwater. The threshold is fixed across
the entire case and all tiles. There is no adaptive per-tile threshold,
morphological postprocessing, connected-component filtering, or spatial halo
that could change tiled versus full scientific meaning.

`before_water.tif` and `after_water.tif` encode 0 nonwater, 1 water, 255 unknown
for the respective date. They retain independently observed pixels. Change
analysis is restricted to the intersection `V = valid_before & valid_after`
on the rectangular AOI grid. `valid_before.tif`, `valid_after.tif`, and
`valid_common.tif` provide explicit, independently countable 0/1 masks.

`change.tif` contains a mutually exclusive, complete partition:

| Code | Meaning | Membership |
|---|---|---|
| 0 | Unknown | outside V |
| 1 | Stable nonwater | V and neither date water |
| 2 | Stable water | V and both dates water |
| 3 | Gained water | V and only after water |
| 4 | Lost water | V and only before water |

The unknown class is a real value, not a GeoTIFF nodata tag. The sum of classes
1–4 must equal the common-valid count. Classes 0–4 must cover every grid pixel.
`stats.json` reports both dates' valid coverage, observed water counts, water
counts restricted to V, unknown area, per-class counts/areas, and net change.

## Area, vectors, and map products

Water area requires a projected grid. Pixel area is
`abs(a*e - b*d) * projected_linear_unit_to_metre^2` from its affine transform.
For the 20 m UTM grids, this is 400 square metres per pixel. These are projected
map-grid areas, not terrain-corrected surface areas. Longitude/latitude degrees
are never treated as square metres.

`changes.geojson` contains gained/lost connected polygons (4-neighbor
connectivity), reprojected to WGS84 longitude/latitude for RFC 7946. Each feature
records its projected-raster pixel count and area in square metres. Areas are
not measured from rounded longitude/latitude output geometry. Empty changes
produce a valid FeatureCollection with no features. `map.png` is a labelled
three-panel before/after/change quicklook with unknown shown in gray. GeoTIFFs
retain exact full-resolution georeferencing.

The three continuous indices are exported for each phase as
`{before,after}_{ndvi,ndwi,mndwi}.tif`. The final product set also
includes the six mask/change rasters, `changes.geojson`, `map.png`, and
`stats.json`. These are ordinary, reusable files without a proprietary service.

## Tile and merge boundary

`water_tile` receives `[row_offset, col_offset, height, width]`. It outputs its
window rasters and `tile.json`, including full-grid metadata, relative file
names, SHA-256 hashes, scale audit records, threshold, and formulas.

`water_merge` reads only `task.json` and the received tile products. It never
opens original imagery or `assets.json`. It validates the case, implementation
version, threshold, product set, file hashes, raster CRS/transform/shape,
nonoverlapping windows, complete full-grid coverage, and class consistency.
Missing, duplicated, overlapping, corrupt, or incompatible tiles fail loudly.
A tile with no valid observations can be merged as unknown; an entire AOI
without a common valid observation fails rather than claiming success.

`water_full` calls the same tile implementation once for the full window and
then the same merge implementation. No alternative full-scene algorithm exists.
The acceptance tolerance is fixed in advance: exact equality for categorical
rasters, absolute 1e-6 for continuous indices and 1e-6 m² for counted grid
areas. These limits are not relaxed after seeing results. Independent acceptance
must recalculate from inputs and outputs without calling these statistics
functions.

## Author execution evidence

Author direct invocation (not a substitute for the common harness or independent
acceptance) used `/workspace/shared/orbitserve-venv/bin/python` with
`PYTHONPATH=src`, calling `run_operator('earthbench_tvdi', case, out, {})` for
both real input cases. Exit code was 0 on 2026-10-16.

External run directory: `/workspace/shared/orbitserve-runs/a02-direct-20261016`.

- question2: 1,307,762 valid TVDI pixels; 367,090 above 0.75;
  28.07009226449461 percent.
- question4: 4,482,532 valid TVDI pixels; 669,532 above 0.75;
  14.936468942106828 percent.

These observations are descriptive results from actual pixels, not embedded
constants in the operator. Water sample execution and the common-harness,
distributed, and independent acceptance records are owned by their actual run
manifests; implementation availability alone is not proof of those experiments.

### Corrected source-data author run

After the source-offset metadata correction and the independent validator's
matching authority-field check, the following direct command completed with
exit code 0 in a new directory (6.59 seconds). The script is an external author
reproduction helper, not a committed test framework or a replacement for the
common harness:

```sh
PYTHONPATH=src /workspace/shared/orbitserve-venv/bin/python \
  /workspace/shared/orbitserve-runs/a02-direct-20261016/run_direct_science.py \
  /workspace/shared/orbitserve-data \
  /workspace/shared/orbitserve-runs/a02-direct-final-20261016T1337
```

Run ID: `a02-direct-final-20261016T1337`.
The directory contains `author_run_summary.json`, individual scientific
products, independent verification JSONs, and `full_tile_comparison.json` for
each water case. Console evidence is in the adjacent
`a02-direct-final-20261016T1337.log` file.

| Real water case | Common-valid pixels | Gained pixels | Lost pixels | Unknown pixels |
|---|---:|---:|---:|---:|
| Lake Mead | 51,877 | 580 | 1,805 | 10,623 |
| Lake Powell | 61,341 | 165 | 5,184 | 1,159 |
| Lake Shasta | 62,228 | 110 | 5,033 | 272 |

Every case uses 62,500 pixels on the original 250 x 250, 20 m grid. All three
full versus four-tile comparisons passed with zero categorical mismatches and
zero continuous-raster difference. Each merge was run against a directory
containing only `task.json`, with tile directories passed separately: no
original imagery or `assets.json` was available in its case input. Independent
source-pixel recomputation passed for both full and merged products. All three
map PNGs were opened and visually inspected. Earth-Bench results remained
unchanged and also passed independent verification.

The earlier `a02-direct-final-20261016T1336` attempt exited 1 because the
independent validator expected different harmonization-authority field names.
Its failure log and outputs are retained; they are not counted as final
acceptance. Earlier water products with the contradictory -0.1 offset are
superseded and must not be used for reported water areas. The run above predates the final narrowing of diagnostic outputs. The final
science release retains only MNDWI, NDVI, and NDWI; water classifications are
unchanged. Integration and four-node experiments remain the responsibility of
their own run evidence.

### Final scope freeze: version 1.0.1

The same direct command was run again with the clean output destination
`/workspace/shared/orbitserve-runs/a02-final-scope-20261016T1339` after narrowing
water outputs to MNDWI, NDVI, and NDWI. Exit code 0; elapsed 5.77 seconds.
All five cases passed independent recomputation and all three full/tiled water
comparisons passed exactly. Counts in the table above were unchanged. Each
water product now contains 12 GeoTIFFs plus GeoJSON, PNG, and JSON, with only
MNDWI contributing to classification. Summary and per-case verification files
are within that directory; the adjacent `.log` preserves console output.

Frozen science SHA-256:
`f0ee0b0e63a7be36721fbfb718e7285a3d6d4a8f152f05a4c49817a52657998e`.
