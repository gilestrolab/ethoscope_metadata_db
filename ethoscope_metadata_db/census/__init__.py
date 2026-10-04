"""Ethoscope census (v2): which databases exist, which metadata claims them.

Modules:
    store: census SQLite schema and access helpers.
    dbindex: index the ethoscope results tree (read-only).
    discover: find and register metadata files in user homes.
    ingest: read csv/tsv/xls/xlsx metadata into normalised rows.
    normalise: dates, machine names, region ids, sex.
    match: metadata rows -> databases with explicit statuses.
    report: claimed / orphan / unmatched views and queries.
    cli: the ``ethocensus`` command.
"""

__version__ = "2.0.0.dev0"
