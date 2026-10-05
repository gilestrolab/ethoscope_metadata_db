# Ethoscope metadata db
This is a package that can be used to organise your ethoscope database.
The core of the package is ethoscope_metadata_db which takes care of 
 - building an internal representation of the ethoscope data
 - handle metadata file associating them to experiments
 - collect information regarding a metadata file and its db dependencies
 - export that information

 A typical use case is to recover all the ethoscope dbs associated to a certain metadata file so that the user can collate them and upload them to a public dataset repository, such as Zenodo.

For instance:
```
ffrom ethoscope_metadata_db.metadata_db import db_organiser, metadata_handler

metadata_filename = 'all_metadata.csv'
datapath = "/mnt/ethoscope_results"

#refresh the content of the ethoscope dbs
db = db_organiser(datapath, refresh=True, csv_path="/opt/ethoscope_metadata/")

#find the relevant files associated to our metadata - this will take a few seconds
meta = metadata_handler(metadata_filename, project='Joyce_2024', authors=['Joyce', 'Blackhurst', 'Falconio', 'French'] )
meta.associate_to_db(db)

#Give me a summary of what you found. Useful to know the size of the whole dataset.
meta.summary

#The followind dbs were not found on our machine
#This usually indicates that the ethoscope had failed but was not removed from the metadata
meta.list_dbs(notfound=True)

#Save to a txt file only the databases we have actually found
meta.export('Joyce_2024_only_dbs.txt')

#Saves all the info for our convenience
meta.export('Joyce_2024_only_dbs.txt', verbose=True)
```

The second class is the **metadata_db_server** which runs a web based UI (deprecated: replaced by
`ethocensus-web`, see below)
Instructions on to set it up and run it can be found [here](https://www.notion.so/giorgiogilestro/The-Metadata-Database-Server-cafac3d16ca04c0b8d4364e7624d7151).

## The ethoscope census (v2, in development)
`ethocensus` indexes every ethoscope database, discovers metadata files in user homes (csv, tsv,
txt, xlsx/xls sheets with ethoscopy's `machine_name`, `date`, `region_id` columns), and matches each metadata row
(one fly) to the run that started on its date, the last of the day unless a `time` column says
otherwise. Every row gets an explicit status (`unique`, `ambiguous`, `date_ambiguous`,
`roi_missing`, `not_found`, ...); copies of a run and passed-over false starts are linked as
`duplicate` and `superseded`. It only ever reads the results
tree and the homes; everything it learns goes into its own SQLite file. See `PLANNING.md`.

```
ethocensus --census census.sqlite scan-dbs /mnt/ethoscope_results
ethocensus --census census.sqlite discover /home          # register candidate metadata files
ethocensus --census census.sqlite ingest                  # or: ingest my_metadata.xlsx
ethocensus --census census.sqlite match
ethocensus --census census.sqlite report --out-dir census_report/
ethocensus --census census.sqlite query "genotype in ['CS', 'w1118'] and stack == 'picamera2'" --out controls.csv
```

The query output uses ethoscopy's metadata columns (`machine_name`, `date`, `time`,
`region_id`) plus `path`, the database file.

When a second copy of the results tree or of the homes holds a few files the main one lacks,
add only those (one path per line, relative to the root) so no run is counted twice; skip
directories by name with `--exclude`:

```
ethocensus --census census.sqlite scan-dbs /mnt/backup/results --files-from missing_dbs.txt
ethocensus --census census.sqlite discover /home --exclude secrets --exclude cache
```

Record dates as three columns, `day`, `month` and `year`: a single date cell is rewritten by
spreadsheets in the computer's locale, and `05/01/2024` can then mean 5 January or 1 May. Files
with a single `date` column are still read: day/month strings follow the order the sheet's own
unambiguous dates show, and a date that stays ambiguous is matched both ways and flagged
`date_ambiguous` if the month-first reading finds a run. Runs are dated by the start recorded in
their METADATA, on the lab's clock (`match --timezone`, default `Europe/London`), not by the
folder stamp, which is sometimes wrong. Each database also records who ran it and where
(`experimenter`, `location`, as typed on the node), which shows whose home an unannotated run
belongs to. `discover --no-txt` skips `.txt` files. rethomics exports (`machine_name`, `datetime`,
`region_id`) are read too: `datetime` gives both the day and the run's start time.

`report --out-dir` also writes `header_counts.csv` (column names in use, accepted or not) and
`date_shapes.csv` (date formats, digits shown as 9), which show what the metadata looks like
without showing any value. Run the tests with `pip install -e ".[test]" && pytest`.

### Configuration, refresh and the web UI
List the trees the census reads in a TOML file (see `census.example.toml`) and refresh everything
in one go: scan, discover, ingest, match and `summarise`, which builds the summary tables the
web UI reads.

```
ethocensus --census census.sqlite refresh --config census.toml
ethocensus --census census.sqlite summarise --config census.toml   # after a manual match
```

The web UI (`pip install -e ".[web]"`) browses runs and metadata files with facets, shows every
run's tubes and annotations, every file's unmatched rows and why, and exports any selection as
ethoscopy metadata. It only reads the census.

```
ETHOCENSUS_DB=/path/to/census.sqlite ethocensus-web        # serves on 127.0.0.1:8095
```

Curation lives in its own file (`ETHOCENSUS_CURATION`, default `curation.sqlite` next to the
census), so the census can be rebuilt without losing it:

- **Labels and projects**: free tags, or projects with a DOI, description and authors, attached to
  runs, metadata files or whole folders. Label any filtered selection of runs or files in one go,
  or accept a suggested project folder. A label on a metadata file also covers the runs it claims.
- **People**: one person gathers the names they appear under (home folders, experimenter names
  typed on the nodes); the UI suggests names that probably belong together.
- **Notes** on runs, files, labels and people, and a **Changes** log where any edit can be undone.

Pages show the essentials first; secondary columns, filters and sections open on request.

It expects to sit behind an SSO proxy (oauth2-proxy with OIDC) that sets `X-Forwarded-Email`;
requests without it are refused. For local development only, set `ETHOCENSUS_REQUIRE_USER=0`.

## Installation
Install with pip

```
#optional pyenv
python -m venv ~/python-envs/ethoscope_db/
source ~/python-envs/ethoscope_db/bin/activate

#install in edit mode
pip install -e .
```
