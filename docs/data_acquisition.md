# Data acquisition and provenance

The downloader operates on public HTTPS endpoints from the local execution
workspace. It downloads no model weights and never modifies an existing project.
Actual inputs are stored outside this repository. The portable default is
`$ORBITSERVE_DATA_ROOT` or `~/.cache/orbitserve/data`.

```bash
python scripts/prepare_data.py --data-root /path/to/external/orbitserve-data
# Small initial science check only:
python scripts/prepare_data.py --data-root /path/to/external/orbitserve-data --cases earthbench
```

Required dependencies are those in the project environment, including rasterio
1.4+ (Python opener support), requests, numpy and pyproj. During this implementation
the interpreter is `/workspace/shared/orbitserve-venv/bin/python` and data root is
`/workspace/shared/orbitserve-data`. No raw TIFFs, downloaded reference answers,
or runtime products belong in Git.

## Verified sources and isolation

- Earth-Agent source: `opendatalab/Earth-Agent`, revision
  `ca1dfaef750654331308a434966d307f1728fd90`.
  The downloader retrieves the pinned `benchmark/question.json` from the official
  raw GitHub endpoint, then sanitizes tasks. It does not invoke its lookup tools.
- Earth-Bench imagery: `Sssunset/Earth-Bench`, revision
  `49ce6b784c038b63b0817cbfa8211eb053b47e37` on Hugging Face. Only question2 and
  question4 TIFFs are downloaded, with exact sizes and LFS SHA-256 validation where
  supplied by the listing. A SHA-256 is computed for every local input regardless.
- Sentinel-2 L2A: Earth Search STAC
  `https://earth-search.aws.element84.com/v1`, collection `sentinel-2-l2a`.
  Pixel bytes are read from the returned public Sentinel COG URLs. Every selected
  complete STAC item is saved outside the repo in `catalog/`.
- question100 is intentionally excluded: its upstream reference trajectory has
  inconsistent band/path usage and it is unnecessary for the specified cases.

Only `earthbench/<question>/{task.json,assets.json,*.tif}` and
`cases/<case_id>/{task.json,assets.json,imagery/}` are runtime inputs. Upstream
reference dialogs, tool traces, choices and answers reside exclusively in the
external `evaluation/` directory. Neither the scientific operators nor node
workers require that directory. The runtime must copy only declared input files
and deny access to evaluation and other nodes' roots; a separate folder name alone
is not an isolation mechanism.

## Acquisition and observation times

Every runtime asset records timezone-aware `acquired_at`, `observation_time`,
`observation_time_precision`, and `original_dtype`. These are source provenance,
not the time a later acceptance run validated a cache.

For the unchanged Earth-Bench originals, `acquired_at` is the earliest successful
HTTP 200 `download` receipt in the retained acquisition ledger whose source URL
and SHA-256 both match the local input. Cache reuse preserves this original time.
An input without such a receipt is downloaded again to establish an actual
receipt; a `cached_file_validated` event is never substituted. The available
observation evidence identifies a date only: `observation_time` retains
`YYYY-MM-DD`, `observation_time_precision` is `date`, and `observation_date` is
preserved. No midnight instant or timezone is invented for these composite dates.

For Sentinel, `observation_time` is the selected STAC item's exact timestamp and
its precision is `instant`. `acquired_at` is when the derived AOI raster was
written and inspected after its bounded source-range reads. Verified crop reuse
preserves this completion time from the matching `crop_complete` receipt.
`acquisition_time_basis` makes these two acquisition meanings explicit. Adding
these JSON provenance fields does not alter TIFF pixels, tags or source revisions.

## Earth-Bench scaling and errata

Both question2 and question4 request **TVDI from supplied NDVI and LST products**,
followed by the fraction above 0.75. Neither pair contains red/NIR bands from which
to recompute NDVI. An NDVI-inspection tool is not a substitute for their TVDI task.

| Case | File | Measured bytes |
|---|---|---:|
| question2 | Sichuan_2021-07-12_LST.tif | 87,263 |
| question2 | Sichuan_2021-07-12_NDVI.tif | 493,087 |
| question4 | Chengdu_2022-08-13_LST.tif | 307,078 |
| question4 | Chengdu_2022-08-13_NDVI.tif | 1,139,510 |

These four source files total **2,026,938 bytes**. NDVI is signed int16 with
NoData -32768 and physical scale 0.0001; LST is uint16 with NoData 0 and physical
scale 0.02 K/DN. These factors are explicitly in each TIFF's **band tags** even
though rasterio's `scales` tuple says 1. Integer value ranges confirm that factors
have not already been applied. `assets.json` retains the tags, integer dtype,
valid sample range and factor. The scientific operator applies each once.

The files are exported on a 30 m EPSG:3857 grid. Source tag resolutions are
**250 m NDVI** and **1,000 m LST**. Oversampling does not create 30 m physical
information. NDVI is a composite rather than a same-instant temperature sample.
No agricultural land-cover mask or independent drought truth is supplied.

question2 evaluation prose incorrectly refers to 2022 and EVI. Actual filenames,
metadata and the original user dialog use **2021-07-12 and NDVI**. The runtime task
is explicitly a corrected derived task, not an unmodified official benchmark
reproduction. No reference percentage is used by the operators or data selection.

## Three paired shoreline cases

The config selects real shoreline AOIs at Lake Mead (Boulder Basin), Lake Powell
(Wahweap Bay), and Shasta Lake (central arms), each a **250 x 250 projected grid
at 20 m**, exactly 5 km x 5 km / 25 km² of projected area. June 2020 and June 2021
provide matched-season reservoir observations. The center is snapped to a stable
20 m UTM grid. This is a physically motivated water-change candidate study, not a
claim about flood accuracy or the cause of water-level changes.

Initial selection searches June candidates, requires complete bands and full
projected square coverage, and selects minimum reported scene cloud fraction.
All candidates and rejection-by-coverage evidence are retained in
`acquisition.jsonl`. Scene cloud fraction is not a substitute for local masking;
local common-valid and water/change counts are separately recorded in
`catalog/*_quality.json`. The final item IDs are pinned in
`configs/data_selection.json` for reproduction. The same six required bands are
included at both dates: B02, B03, B04, B08, B11 and SCL, **36 rasters total**.

Large full-scene reflectance downloads are avoided. A range-aware rasterio opener requests
bounded 256 KiB HTTP blocks, counts and hashes returned body bytes, and reads only
an AOI window with a two-native-pixel interpolation margin. For a very small,
highly compressed SCL COG, the needed header and image blocks can incidentally
cover the whole object; the logs reveal that rather than calling it a partial
byte download. No whole-scene image read is requested. It rejects endpoints
that ignore HTTP Range. This counts actual response bodies, including requests
for one-byte file-size discovery; it does not estimate TLS or HTTP header costs.
Metadata request bodies are logged separately. Initial exploratory downloads are
not claimed as part of the scripted COG transfer measurement.

Each derived crop retains unscaled DN values: continuous bands are bilinearly
aligned into float32; SCL uses nearest-neighbor uint8. The stored physical
scale comes from that exact STAC asset's `raster:bands`. Effective offset also
uses the item-level `earthsearch:boa_offset_applied` flag, as explained below. Source dtype, grid, window, NoData, observation time, item ID, URL,
acquisition time and local hash are recorded. Source full-scene SHA-256 is
explicitly null because no complete source-object hash was computed; all obtained byte
ranges and local AOI products are hashed. No physical scale is pre-applied.

The scientific workflow applies a fixed MNDWI threshold of 0 and excludes invalid
reflectance, zero denominators and SCL classes outside 4/5/6/7. Only common-valid
pixels contribute to before/after change. The input-quality screen reports
whether change candidates exist, without manipulating any pixels, threshold,
dates, or validity mask to force a nonempty result.

## Evidence and reproducibility

- `acquisition_manifest.json`: run ID, revisions, source URLs, sizes, hashes,
  every asset, and the selected item IDs.
- `acquisition.jsonl`: download/cache validation, STAC selection, per-range
  offsets/status/actual body bytes/hash, crop completion and case completion.
- `catalog/`: complete selected STAC items, HF file listings and quality screens.
- `earthbench/` and `cases/`: genuine inputs with sanitized task and asset metadata.
- `evaluation/`: reference-only material, never supplied to science or workers.

The acquisition stage establishes provenance and input quality, not independent
scientific or runtime acceptance. Final validation must verify hashes, pixel grids,
common-valid conservation, physical scaling and full/tile equivalence independently.

## Provider offset defect found and corrected during cross-review

All six selected scenes are PB05.00 reprocessings, even though their observation
dates precede 2022. Each item has `earthsearch:boa_offset_applied=true` while its
reflectance `raster:bands.offset` still reports -0.1. Blindly trusting that stale
asset field would subtract the offset twice. The [provider maintainer's
explanation](https://github.com/Element84/earth-search/issues/9) identifies the
item flag as authoritative; the [later defect report](https://github.com/Element84/earth-search/issues/71)
shows the inconsistency persists.

The downloader therefore records both reported scale/offset and effective
scale/offset. Only when the exact item's harmonization flag is true is the
effective reflectance offset set to zero. This is a processing-provenance rule,
not a calendar or result-dependent adjustment. With a false flag the declared
asset offset is used. A missing flag combined with a post-PB04 nonzero offset
fails rather than guessing. SCL remains unscaled categorical data.

Every runtime asset carries `reported_scale`, `reported_offset`,
`earthsearch_boa_offset_applied`, `processing_baseline`, `scaling_version`,
`scaling_authority_url`, `scaling_rationale`, and its unchanged original
`source_raster_band`. The final effective scale is 0.0001 and offset is 0 for all
30 reflectance AOI rasters. The TIFF band tags agree. No scientific worker needs
access to catalog or reference directories to know this provenance.

The historical provisional water run used the stale offset and was invalidated.
The correction preserves raw pixels and records the harmonization authority.
The fresh acquisition and scientific runs below use the corrected metadata.

## Fresh acquisition and cache verification, 2026-10-17

The previous environment's full input/run directories were unavailable when the
original checklist was reopened. Public inputs were acquired again. Cold run
`acquire-20261017T001751` exited 0; cache normalization run
`acquire-20261017T002357` also exited 0 without changing any of the 40 TIFF hashes.
An earlier interrupted acquisition is retained as a failed attempt, not counted
as completion. Its successful COG responses remain included in cumulative traffic.

The fresh input set contains **40 TIFFs, 5,907,906 bytes**: four original
Earth-Bench TIFFs and 36 aligned Sentinel AOI crops. Cumulative successful COG
HTTP response bodies total **87,956,205 bytes**, excluding HTTP/TLS overhead,
metadata responses and exploratory access. Cache normalization used **0 new COG
body bytes**. These are measured counts, not the provisional download budget.

The 37,128-byte difference from the prior TIFF encoding is explained by its
metadata-update layout. Replaying the old writer and offset-tag repair on the
fresh arrays recreates every one of the 36 historical hashes and sizes. Fresh
hashes and acquisition times remain distinct from historical ones. The current
acquisition manifest SHA-256 is
`5ca1a205fe4daf43b0604d8dff3103480e73c8a7fc1c77be3cfe15a09c580a84`.

Restoration passed 393 checks, including all 40 timestamp provenance checks;
five focused receipt regressions also passed. The independent reviewer then
ran the exact README prepare-data command against a separate cache copy:
`acquire-20261017T002659`, exit 0, unchanged hashes, 0 new COG body bytes.

| AOI | Common-valid pixels | Common-valid fraction | Gained candidates | Lost candidates |
|---|---:|---:|---:|---:|
| Lake Mead | 51,877 | 0.830032 | 580 | 1,805 |
| Lake Powell | 61,341 | 0.981456 | 165 | 5,184 |
| Shasta Lake | 62,228 | 0.995648 | 110 | 5,033 |

These are screening counts from real inputs, not external ground-truth labels.
The [current acceptance report](acceptance_report.md) records fresh independent
scientific verification. Full acquisition ledgers, catalog metadata, timestamp
proofs and historical-encoding comparison are retained in the separate evidence
archive under `shared/orbitserve-data/` and
`shared/orbitserve-restoration-20261017/`; raw TIFFs are acquired using the
command above and are excluded from both repository and delivery archive.
