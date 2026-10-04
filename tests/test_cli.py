import subprocess
import sys

import pandas as pd
import pytest

from conftest import write_text
from ethoscope_metadata_db.census import cli, ingest


def run(census_path, *args):
    return cli.main(["--census", str(census_path), *args])


def test_full_pipeline(results_root, homes_root, tmp_path, capsys):
    census = tmp_path / "census.sqlite"
    assert run(census, "scan-dbs", results_root) == 0
    assert "seen=6" in capsys.readouterr().out
    assert run(census, "discover", homes_root) == 0
    assert "sheets awaiting ingest: 3" in capsys.readouterr().out
    assert run(census, "ingest") == 0
    assert "rows=16" in capsys.readouterr().out
    assert run(census, "match") == 0
    assert "unique=10" in capsys.readouterr().out

    out_dir = tmp_path / "report"
    assert run(census, "report", "--out-dir", str(out_dir)) == 0
    assert "orphan" in capsys.readouterr().out
    assert (out_dir / "db_claims.csv").exists()

    out = tmp_path / "cs.csv"
    assert run(census, "query", "genotype == 'CS' and stack == 'picamera2'", "--out", str(out)) == 0
    tubes = pd.read_csv(out)
    assert set(tubes["genotype"]) == {"CS"} and {"path", "region_id"} <= set(tubes.columns)


def test_query_for_ethoscopy(results_root, homes_root, tmp_path):
    census = tmp_path / "c.sqlite"
    for args in (("scan-dbs", results_root), ("discover", homes_root), ("ingest",), ("match",)):
        run(census, *args)
    out = tmp_path / "meta.csv"
    assert (
        run(census, "query", "machine_name == 'ETHOSCOPE_007'", "--ethoscopy", "--out", str(out))
        == 0
    )
    # Read it back the way ethoscopy does: no NaN allowed, exact columns.
    meta = pd.read_csv(out)
    assert not meta.isna().any().any()
    assert list(meta.columns) == [
        "machine_name",
        "date",
        "time",
        "region_id",
        "genotype",
        "sex",
        "treatment",
    ]
    assert set(meta.loc[meta["date"] == "2024-01-10", "time"]) == {"15-00-00"}


def test_query_to_stdout_and_bad_query(results_root, homes_root, tmp_path, capsys):
    census = tmp_path / "c.sqlite"
    for args in (("scan-dbs", results_root), ("discover", homes_root), ("ingest",), ("match",)):
        run(census, *args)
    capsys.readouterr()
    assert run(census, "query", "region_id == 3") == 0
    assert capsys.readouterr().out.startswith("meta_row_id,")
    assert run(census, "query", "this is not python") == 2
    assert "query failed" in capsys.readouterr().err


def test_ingest_named_file(tmp_path, capsys):
    path = write_text(str(tmp_path / "m.csv"), "date,machine_name,region_id\n2024-01-05,7,1\n")
    assert run(tmp_path / "c.sqlite", "ingest", path, "--home", "manual") == 0
    assert "rows=1" in capsys.readouterr().out


def test_census_argument_required():
    with pytest.raises(SystemExit):
        cli.main(["scan-dbs", "/nowhere"])


def test_console_script_module_runs(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ethoscope_metadata_db.census.cli",
            "--census",
            str(tmp_path / "c.sqlite"),
            "report",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0 and "databases indexed: 0" in result.stdout


@pytest.mark.parametrize(
    "lines,expected",
    [
        (["title only", "a;b;c", "1;2;3"], ";"),
        (["a,b,c", "1,2,3"], ","),
        (["a\tb", "1\t2"], "\t"),
        (["just one column", "x"], ","),
    ],
)
def test_guess_delimiter(lines, expected):
    assert ingest.guess_delimiter(lines) == expected
