# ethoscope_metadata_db v2: the ethoscope census

## Goal
Know, for every ethoscope SQLite database we hold, which experiment it belongs to and what was in each tube (genotype, sex, treatment, ...), and know which databases no metadata accounts for (orphans). The census is the basis for any cross-experiment analysis, for example a noise reference built only from wild-type, unstimulated controls.

Metadata already exists: every analysis goes through the ethoscope-lab (JupyterHub) homes, where users keep CSV / TSV / XLS / XLSX metadata files (each row = one fly; typical columns date, machine_name, region_id, genotype, sex, treatment...). v1 relied on users uploading those files and few did, so v2 *discovers* them instead.

## Principles
- Read-only on user data. Homes and the results tree are never modified. The census stores paths, hashes, and parsed rows in its own SQLite file.
- Never resolve ambiguity silently. Every row-to-database match carries a status (unique, ambiguous, not_found, superseded, roi_missing, ...). v1's idxmax "keep the largest file" behaviour is removed.
- Raw values are always kept next to normalised ones.
- Provenance for every row: source file, sheet, owner (home), mtime, content hash.
- Deterministic and re-runnable: a re-scan updates changed files (by hash) only.

## Architecture
ethoscope_metadata_db/census/
  store.py      SQLite schema + access
  dbindex.py    walk the results tree; parse paths; read each .db METADATA (open read-only: sqlite3 "file:{quoted path}?mode=ro&immutable=1" — skip and record files with a non-empty -wal)
  discover.py   walk homes; find tabular files; score "is this ethoscope metadata?"
  ingest.py     read csv/tsv/xls/xlsx (all sheets); column aliasing; value normalisation
  normalise.py  dates, machine names, region ids, sex, (later) genotype vocabulary
  match.py      metadata rows -> .db files with explicit status
  report.py     claimed / orphan / unmatched / multiply-claimed views
  cli.py        `ethocensus` (scan-dbs, discover, ingest, match, report, query)
Keep v1 metadata_db.py / server until the web app is ported. Deps: pandas, openpyxl, xlrd; Python >= 3.10; declare them (v1 setup.py declares none).

Results tree path pattern: <machine_id>/<machine_name>/<YYYY-MM-DD_HH-MM-SS>/<YYYY-MM-DD_HH-MM-SS>_<machine_id>.db (v1 parser: metadata_db.py:105-117).
Useful METADATA keys inside each .db: machine_id, machine_name, date_time (unix start), version, hardware_info (python-literal dict: kernel, camera — sometimes a plain string —, pi_version), selected_options (python-literal-ish string: tracker class, interactor class/kwargs), gain_setting, maxfps_setting, tracker_class, stop_date_time (may be absent). ROI tables: ROI_MAP(roi_idx, x, y, w, h), ROI_n(t ms, x, y, ..., is_inferred stored as tinyint OR text '0'/'1').

### SQLite schema (first cut)
- db_file(id, path, machine_id, machine_name, start_utc, size, mtime, sw_version, kernel, camera_sensor, tracker, gain, maxfps, n_rois, duration_h, interactor, readable, error); schema 3 adds experimenter and location (METADATA `experimental_info`: who ran it, which incubator)
- meta_file(id, path, home, sheet, sha256, mtime, n_rows, columns_raw, score, status)
- meta_row(id, meta_file_id, row_idx, raw_json, date, machine_name, region_id, sex, genotype_raw, treatment_raw, other_json)
- match(meta_row_id, db_file_id, status, method, note) — many-to-many
- vocab(kind, raw, normalised, curated_by) — phase 3

### What counts as metadata
Files read: csv, tsv, txt (rethomics-era metadata was often written with R's `write.table`; Giorgio, 2026-10-04) and every sheet of xlsx/xlsm/xls. What ethoscopy accepts (`link_meta_index`): the first row has the columns `machine_name`, `date` and `region_id`, spelled exactly, plus an optional `time`. No column aliasing, no title rows (on real data, aliasing would add 53 of ~4,150 sheets). Descriptive columns `genotype`, `sex`, `treatment` are picked up case-insensitively; every other column is kept verbatim.

rethomics (scopr) exports are accepted too (Giorgio, 2026-10-04): `machine_name`, `datetime`, `region_id` with no `date`; `datetime` gives the day and the run's start time. `ingest --reparse` re-checks rejected sheets' stored headers when the rules change.

Dates (Giorgio, 2026-10-04): the unambiguous way to record a date is three columns `day`, `month`, `year`, which survive spreadsheets that rewrite a date cell in the computer's locale. When all three are present they stand in for `date` (and win over it; disagreements are noted). ethoscopy and rethomics are to accept them and to warn when only `date` is given.

### Matching rules
1. Normalise machine_name (ETHOSCOPE_007 = ETHOSCOPE_7 = 007 = 7) and the date. A single `date` column: ISO strings, Excel serials and datetime cells are unambiguous. Day/month strings (`05/01/2024`) are read in the order the sheet's own unambiguous strings show (a part above 12 can only be a day); a sheet with no or mixed evidence reads them day-first (lab convention) and keeps each ambiguous one's month-first reading too (`date_status` `ambiguous`, `date_alt`). On real data, 25 sheets are month-first and 89 have no evidence.
2. The metadata date is the date the experiment started. A run's start is the `date_time` in its METADATA, on the lab's clock (`Europe/London`, `match --timezone`); the results-tree stamp is used only when METADATA cannot be read. Stamps are unreliable: 269 differ from METADATA by date, and users' metadata follows METADATA (127 dbs) far more than the stamp (12). When no run starts that day by METADATA, a run whose stamp has that date is accepted and flagged (method `...+path_date`). A db merely still running on that date is not a candidate.
3. Files of one machine with the same METADATA start (to the second) are copies of one run: 863 groups on turing, mostly the same run under two folders whose stamps differ by 1 h (local vs UTC) or by minutes, plus `_backup_` and `_NN_` files; 731 groups hold copies of different sizes (partial syncs). The readable file with the standard name is used, the largest (most complete) if there are several, and the others are linked as `duplicate`; with no readable standard file, all are `ambiguous`. This is not v1's "keep the largest": it only ever chooses among copies of one recording, never among different runs of a day.
4. Several runs starting the same day (false starts, early crashes): the experiment is the last of the day (`last_of_day`), never an empty (0-byte) file while a real one exists; the earlier ones are recorded as `superseded`.
5. A `time` column picks the run instead: exact match on the local start or the path stamp (`start_time`); failing that, the run within a tolerance (`start_time_approx`, flagged); several runs within tolerance: `ambiguous`, all listed.
6. An ambiguous date whose month-first reading finds a run is `date_ambiguous`, with every candidate of both readings listed.
7. region_id must exist in that db's ROI_MAP, otherwise `roi_missing`.

### Census outputs
Database categories: single / multiple (claimed), tentative, duplicate, superseded, empty (0 bytes), misfire (unclaimed and <= 5 MB, or a known length < 24 h: failed starts and crashes; Giorgio, 2026-10-04) and orphan.
- dbs claimed by one source, by several (do they agree on genotype?), superseded same-day runs, or by none (orphans);
- rows not found or ambiguous;
- summaries by year, home, machine, stack (legacy vs picamera2 from the kernel: '+rpt' or '-v8' = picamera2), tracker;
- `query` (e.g. genotype in {CS,w1118} & interactor == DefaultStimulator & stack == picamera2) returns db + tube lists loadable by ethoscopy / scopr.

### Known v1 bugs (don't carry over)
- hard-coded DB_FOLDER='/opt/ethoscope_metadata' (metadata_db.py:36, :195);
- undefined fp in make_index_file (:143);
- crawl stores the builtin hash (:441);
- missing socket/traceback imports in server error handlers (server:208-213);
- bare except + idxmax in find_db_file (:167-178).

### Context
This came out of the 2026-10 picamera2 camera-noise investigation. A behaviour-grounded noise reference needs annotated controls; mutants and sleep-deprived animals must not be folded into it.
