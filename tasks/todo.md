# Tasks: ethoscope census (v2). Added 2026-10-04.
## Phase 0: discovery (runs on turing, NOT in this session)
- [ ] Inventory .db files (path, size, mtime, opens?) and csv/tsv/xls/xlsx in homes. Score likely metadata files; tabulate column variants and date formats; report before freezing the schema.
## Phase 1: core library (branch v2-census)
- [x] store.py schema
- [x] dbindex.py (path parser + per-db METADATA)
- [x] discover.py + ingest.py (all formats/sheets, aliasing, provenance, re-scan by hash)
- [x] normalise.py (dates: dd/mm/yyyy enforced, ISO, Excel serial; machine names, region ids, sex)
- [x] match.py (coverage-based, start-time disambiguation, explicit statuses, many-to-many)
- [x] report.py
- [x] pytest per module with synthetic dbs + messy metadata files
## Phase 2: CLI
- [x] ethocensus scan-dbs | discover | ingest | match | report | query
## Phase 3: vocabulary
- [ ] genotype/treatment mapping table with suggested groupings for human review; raw kept
## Phase 4: first use
- [ ] controls-only bona-fide sleep noise reference (separate repo/script)
## Later
- [ ] port or retire the web app; identify ~50 picamera2 recordings lacking camera model in METADATA
## Discovered During Work
Phases 1-2 built on synthetic data only (2026-10-04); every item below should be checked against Phase 0 findings.
- [x] Matching follows Giorgio's rule: metadata date = start date of the run; several runs that day -> last of the day (earlier ones `superseded`); a `time` column picks a run.
- [x] What counts as metadata = ethoscopy's hard-coded columns (machine_name, date, region_id, optional time), exact spelling, header on the first row.
- [ ] ethoscopy's `link_meta_index` without a `time` column keeps the *largest* same-day file, not the last one. The census writes a `time` column in `query` output so ethoscopy loads the intended run; consider aligning ethoscopy itself.
- [ ] Schema grew beyond the first cut. db_file: path_start, pi_version, stack, roi_ids (JSON list, needed for the roi_missing check), interactor_kwargs, scanned_at. meta_file: size, column_map, header_row, note. meta_row: date_status (ok / invalid / missing), start_time.
- [x] Dates: dd/mm/yyyy is enforced (Giorgio, 2026-10-04); month-first strings are `invalid` and the sheet note counts them.
- [ ] ethoscopy bug: `misc/validate_datetime.py` tries `pd.to_datetime` first, which reads `05/01/2024` month-first (1 May). It falls back to dd/mm only when some day in the column is > 12, so an all-days-<=-12 column is silently read as mm/dd, against the lab rule.
- [ ] Decision to review: a `time` that matches no stamp exactly is accepted within 90 min (`start_time_approx`, `--time-tolerance` to change; 0 = exact only, as ethoscopy).
- [ ] File formats are read leniently (any of , ; tab | delimiters, cp1252, xlsx/xls sheets) even though ethoscopy itself reads comma-separated csv only; columns are strict.
- [ ] Unreadable dbs (non-empty WAL, corrupt) are retried on every scan-dbs; readable unchanged ones (size+mtime) are skipped.
- [ ] No pruning yet: dbs or metadata files that disappear stay in the census. Add a `--prune` option.
- [ ] duration_h falls back to the last ROI row's t (by rowid) when stop_date_time is absent; verify on real dbs that rowid order follows t.
- [ ] Docker/Dockerfile still uses python:3.8; the census needs >= 3.10.
- [ ] Tested on Python 3.11 only.
