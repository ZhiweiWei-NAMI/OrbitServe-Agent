# Scientific methods and reproducibility boundaries

Implementation: `src/orbitserve/science.py`, version `orbitserve-science-1.0.2`.
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
three-panel real-imagery overlay: before RGB with water candidates, after RGB
with water candidates, and after RGB with gained/lost candidates. Unknown is
shown in gray. GeoTIFFs retain exact full-resolution georeferencing.

### Real RGB display products and overlays

`water_tile` constructs `before_rgb.tif` and `after_rgb.tif` directly from the
already-read, grid-aligned **physical** B04/B03/B02 values (red/green/blue).
The existing asset-specific scale and offset have already been applied once;
neither the display encoder nor merge reapplies them. The same fixed linear
display stretch is used for every date, tile and AOI:

`display = uint8(round_half_even(255 * clip(reflectance / 0.30, 0, 1)))`.

Reflectance 0 maps to 0 and 0.30 maps to 255. Gamma is 1; there is no histogram,
percentile, per-date or per-tile contrast adaptation. This is an explicitly
chosen display mapping, not a change to reflectance, water thresholds or other
quantitative outputs. Very bright surfaces can saturate. Quantized display
values must not be used to recompute indices or areas.

Each display GeoTIFF has four uint8 bands, color interpretations
red/green/blue/alpha, descriptions `B04`, `B03`, `B02`, `phase-valid alpha`,
and no nodata value. Alpha is 255 exactly on the existing phase-valid mask
(all required finite spectral bands, accepted SCL, finite MNDWI); otherwise
alpha and all three display channels are 0. Thus valid black pixels are
distinct from invalid observations. The per-phase validity is unchanged.
The original grid and tile-window transform are retained. The `rgb_display`
GeoTIFF tag and tile/final JSON metadata record the fixed mapping, band order,
invalid rule, colors and opacity. Source item IDs, URLs, hashes, observation
times and applied scale/offset remain in the existing input-scaling provenance.

The PNG uses the received display products as its image base. On before and
after panels, water candidates receive blue `(35,108,177)` at opacity 0.35.
On the third panel, gained water receives green `(20,169,145)` and lost water
orange-red `(218,89,56)`, each at opacity 0.50; unchanged pixels retain the
after-date RGB. Blending is `round_half_even((1-opacity)*RGB + opacity*color)`.
Invalid pixels on each date, and pixels outside the common-valid mask on the
change panel, are opaque gray `(165,165,165)`. These are unknown, never land.

For independent image verification, the RGB canvas is 1000 by 480 pixels.
Three 300 by 300 image panels start at `(20,70)`, `(350,70)`, `(680,70)`.
Each full-grid overlay is resized with nearest-neighbor **after** blending;
labels and legend are outside those panel rectangles. Full-resolution display
GeoTIFFs preserve the grid pixels independently of the browsable PNG.

The three continuous indices are exported for each phase as
`{before,after}_{ndvi,ndwi,mndwi}.tif`. The final product set also
includes the six mask/change rasters, two RGBA display rasters,
`changes.geojson`, `map.png`, and
`stats.json`. These are ordinary, reusable files without a proprietary service.

## Tile and merge boundary

`water_tile` receives `[row_offset, col_offset, height, width]`. It outputs its
window rasters and `tile.json`, including full-grid metadata, relative file
names, SHA-256 hashes, scale audit records, threshold, and formulas. Both RGBA
display rasters are actual tile products included in this manifest and in the
same transferred artifact bundle as the scientific rasters.

`water_merge` reads only `task.json` and the received tile products. It never
opens original imagery or `assets.json`. It validates the case, implementation
version, threshold, product set, file hashes, raster CRS/transform/shape,
nonoverlapping windows, complete full-grid coverage, and class consistency.
It additionally checks the display mapping, band types/descriptions/color
interpretations, and equality of RGB alpha with the phase-valid mask. It
reconstructs the full-grid RGBA products from received tile windows; it never
opens raw imagery to obtain a basemap. Every water tile and merged result has
14 GeoTIFFs: the unchanged 12 scientific rasters plus the two display rasters.
Missing, duplicated, overlapping, corrupt, or incompatible tiles fail loudly.
A tile with no valid observations can be merged as unknown; an entire AOI
without a common valid observation fails rather than claiming success.

`water_full` calls the same tile implementation once for the full window and
then the same merge implementation. No alternative full-scene algorithm exists.
The acceptance tolerance is fixed in advance: exact equality for categorical
rasters and all RGBA bytes, absolute 1e-6 for continuous indices and 1e-6 m² for counted grid
areas. These limits are not relaxed after seeing results. Independent acceptance
must recalculate from inputs and outputs without calling these statistics
functions.

## Version history and fresh execution evidence

The [previous published V0 revision](https://github.com/ZhiweiWei-NAMI/OrbitServe-Agent/commit/153353920dcda1210d0753aae4c1a9d5a19cb4b0)
used science version 1.0.1 and a categorical quicklook. Its external run directory
was absent when the original checklist was reopened. Historical summaries are
not counted as evidence for the real-imagery-overlay requirement.

Version 1.0.2 adds the actual RGB display products and semitransparent overlays.
It leaves indices, validity, water/change classes, areas and vectors unchanged.
Fresh public inputs and every E0–E6 experiment were executed again on 2026-10-17.
The lead suite `20261017T002642Z-acceptance-a649ed0d` and independent reviewer suite
`20261017T002828Z-acceptance-cbb560f2` each exited 0 and passed 248 top-level checks.
See the [current acceptance report](acceptance_report.md),
[independent review](independent_review.md), and
[original-checklist audit](../evidence/original_checklist_audit.json).

Additional focused author proofs are under the externally delivered evidence
prefix `shared/orbitserve-overlay-20261017/`:

| Case | Fresh proof run | Independent full / tiled checks |
|---|---|---:|
| Lake Mead | lake_mead-20261017T002441 | 700 / 718, all PASS |
| Lake Powell | lake_powell-20261017T002443 | 690 / 708, all PASS |
| Lake Shasta | lake_shasta-20261017T002444 | 340 / 358, all PASS |

For every case, full/tiled versions of all 14 rasters and the PNG are identical.
The merge case contains only task.json; received tile products supply display
pixels. A fresh version 1.0.1 comparison on these same inputs matches all 12
quantitative arrays, statistics and GeoJSON exactly. The independent verifier
reconstructs RGBA values from raw physical reflectances and reconstructs all
three PNG panels; disposable RGB, PNG and source-hash mutations are rejected.
All three overlays were visually inspected and matched by hash to final ground
products. The first alpha-interpretation failure, subsequent correction and
negative checks are retained in the external evidence archive, not counted as
successful experiments. These focused proofs supplement the real four-node
and common-harness runs above.
