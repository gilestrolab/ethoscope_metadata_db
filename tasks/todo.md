# Tasks: ethoscope census (v2). Added 2026-10-04.
Site-specific findings from the first full run (counts per person, paths, decisions about
individual datasets) are kept in a private run log next to the census file, not in this
public repository.

## Phase 0: discovery
- [x] Inventory .db files and tabular files in the homes; column variants and date formats tabulated (`report`: `header_counts.csv`, `date_shapes.csv`).
- [x] First full run on turing (2026-10-04/05): ~21.9k databases, ~7.7k metadata files, 2.78M rows.
## Phase 1: core library (branch v2-census)
- [x] store.py schema (now schema 3, migrated in place from 1 and 2)
- [x] dbindex.py (path parser + per-db METADATA and ROI_MAP; tracking tables never read)
- [x] discover.py + ingest.py (csv/tsv/txt/xlsx/xls, all sheets, provenance, re-scan by hash)
- [x] normalise.py (dates, machine names, region ids, sex)
- [x] match.py (explicit statuses, many-to-many)
- [x] report.py
- [x] pytest per module with synthetic dbs + messy metadata files
## Phase 2: CLI
- [x] ethocensus scan-dbs | discover | ingest | match | report | query
## Phase 3: vocabulary
- [ ] genotype/treatment mapping table with suggested groupings for human review; raw kept. Many tubes carry different genotype strings in different files.
## Phase 4: first use
- [ ] controls-only bona-fide sleep noise reference (separate repo/script); "unstimulated" = DefaultStimulator.
## Web UI (2026-10-05; plan approved by Giorgio)
Replace the v1 bottle UI: navigable at the current scale, flexible project names / labels / usernames, summaries of claimed and orphan databases by date, user, machine. FastAPI + Jinja2 + htmx, server-side paging; SSO via Keycloak (oauth2-proxy); curation in a separate SQLite file.
- [x] Phase 1a, census: `summarise` (db_summary, file_summary, genotype_summary; 53 s on turing), WAL, `refresh --config census.toml`
- [x] Phase 1b, read-only UI: Overview, Runs (+ run page), Files (+ file page), Search, Export; tests; on the real census every page < 100 ms; checked visually (desktop and 390 px)
- [x] SSO (2026-10-14): reuses the lab's shared oauth2-proxy/Keycloak at the edge (no new client); the app honours the user header only from the proxy's address (ETHOCENSUS_TRUSTED_PROXIES) and accepts edits from the public host (ETHOCENSUS_PUBLIC_HOSTS)
- [x] Phase 2, curation: labels/projects (bulk from any selection, folder suggestions), people + aliases (alias-group suggestions incl. first.last <-> flast), notes, audit log with undo; label/person facets; membership rebuilt after edits and census refreshes
- [x] Simplified presentation (Giorgio, 2026-10-05): secondary columns behind "More columns", secondary filters under "More filters", facet values beyond six under "more", overview breakdowns and run/file detail sections collapsed; empty tube columns hidden
- [x] Docker deployment (2026-10-14): Docker/census-web (python 3.12), bound to the proxy-facing address and localhost only
- [x] Nightly job (2026-10-14): deploy/nightly.sh (systemd user timer census-nightly): census refresh, then backups of curation, census (when changed) and metadata files (ethocensus archive-metadata, content-addressed); optional offsite to Garage; key in an encrypted systemd credential
- [ ] Phase 3: check-my-metadata validator + save-as-source (v2 has no uploads; v1 /save retired)
## Later
- [ ] identify picamera2 recordings lacking a camera model in METADATA

## Decisions (Giorgio, 2026-10-04/05)
- [x] Metadata = ethoscopy's exact columns (`machine_name`, `date`, `region_id`, optional `time`), header on the first row. Also accepted: `day`/`month`/`year` instead of `date` (the future-proof form) and rethomics/scopr exports (`datetime` instead of `date`).
- [x] Single `date` column: day/month order inferred per sheet from its unambiguous dates; ambiguous rows keep both readings and are `date_ambiguous` when the month-first reading finds a run.
- [x] A run's date and time come from its METADATA start on the lab clock (`Europe/London`); folder stamps are sometimes wrong and are a flagged fallback (`+path_date`).
- [x] Files of one machine with the same METADATA start are copies of one run: the readable standard-named file (largest if several) is used, others linked as `duplicate`. The pick among copies is provisional where rows were duplicated by a firmware bug.
- [x] Several runs on one day: the last non-empty one; earlier ones `superseded`.
- [x] `.txt` files are read by default (`--no-txt` to skip).
- [x] Each database records `experimenter` and `location` from METADATA `experimental_info`.
- [x] Unclaimed runs <= 5 MB, or with a known length < 24 h, are reported as `misfire`.
- [x] The census may stay in main if general enough for others.

## Discovered During Work
- [x] Real-data fixes: dict-valued `pi_version`; non-UTF-8 file names skipped and counted (`bad_name`); run-stamp dates; new-firmware `selected_options` (OrderedDict repr) parsed instead of guessed; R `write.table` row names aligned; POSIX-seconds `datetime`; path lists read with surrogateescape.
- [x] `--files-from`, `discover --exclude`, `ingest --reparse` (also re-checks rejected headers), `match --timezone`.
- [ ] Import true run lengths (ROI_1 max t) and duplicated-row factors from an external per-file scan; the census itself reads METADATA and ROI_MAP only. Then misfire = length < 24 h, and runs with duplicated rows flagged unsafe for velocity.
- [ ] Patch ethoscopy and rethomics to read `day`/`month`/`year` and warn when only `date` is given. Fold in the ethoscopy bug: `misc/validate_datetime.py` reads all-days-<=-12 columns month-first.
- [ ] ethoscopy's `link_meta_index` without `time` keeps the largest same-day file; the census writes `time` in `query` output.
- [ ] `--prune`: databases or metadata files that disappear stay in the census.
- [ ] Paths that are not valid UTF-8 cannot be stored (files are skipped and counted).
- [ ] Sheets annotating videos (video_name / scorer_name columns) claim no tracking db; mark them so they don't count as not_found.
- [ ] behavpy `id`-only tables (stamp + machine id + region) could be matched by machine id; not done.
- [ ] R objects (.rds/.RData) and inline metadata in scripts/notebooks are not read.
- [ ] Decision to review: a `time` that matches no run exactly is accepted within 90 min (`start_time_approx`).
- [x] Docker/Dockerfile still uses python:3.8 (v1 only); v2 has its own image in Docker/census-web.
