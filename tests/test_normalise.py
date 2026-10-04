import datetime as dt
import math

import pandas as pd
import pytest

from ethoscope_metadata_db.census.normalise import (
    clean_text,
    is_blank,
    kernel_stack,
    normalise_machine_name,
    normalise_region_id,
    normalise_sex,
    parse_date,
    parse_time,
)

JAN5 = dt.date(2024, 1, 5)


class TestParseDate:
    @pytest.mark.parametrize(
        "value",
        [
            "05/01/2024",
            "5/1/2024",
            "05-01-2024",
            "05.01.24",
            "2024-01-05",
            "2024/01/05",
            "2024-01-05 00:00:00",
            "20240105",
            dt.datetime(2024, 1, 5, 13, 30),
            dt.date(2024, 1, 5),
            pd.Timestamp("2024-01-05"),
            45296,
            45296.0,
            "45296",
        ],
    )
    def test_accepted_forms(self, value):
        assert parse_date(value) == (JAN5, "ok")

    def test_always_day_first(self):
        # dd/mm/yyyy is enforced: never read month-first, whatever the values.
        assert parse_date("01/05/2024") == (dt.date(2024, 5, 1), "ok")
        assert parse_date("25/03/2024") == (dt.date(2024, 3, 25), "ok")

    def test_month_first_strings_are_invalid(self):
        assert parse_date("03/25/2024") == (None, "invalid")

    @pytest.mark.parametrize("value", [None, "", "   ", float("nan"), pd.NaT])
    def test_missing(self, value):
        assert parse_date(value) == (None, "missing")

    @pytest.mark.parametrize("value", ["yesterday", "31/31/2024", "2024-02-30", 12, "1.5"])
    def test_invalid(self, value):
        assert parse_date(value) == (None, "invalid")


class TestParseTime:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("09:30", "09:30:00"),
            ("9.30", "09:30:00"),
            ("09-30-15", "09:30:15"),
            ("9h30", "09:30:00"),
            (dt.time(9, 30), "09:30:00"),
            (dt.datetime(2024, 1, 1, 9, 30), "09:30:00"),
            (0.5, "12:00:00"),
        ],
    )
    def test_forms(self, value, expected):
        assert parse_time(value) == expected

    @pytest.mark.parametrize("value", [None, "", "25:00", "noon", "0.5"])
    def test_unreadable(self, value):
        assert parse_time(value) is None


class TestMachineName:
    @pytest.mark.parametrize(
        "value",
        ["ETHOSCOPE_007", "ETHOSCOPE_7", "ethoscope 7", "Ethoscope-07", "007", "7", 7, 7.0, "7.0"],
    )
    def test_numbered(self, value):
        assert normalise_machine_name(value) == "ETHOSCOPE_007"

    def test_three_digit_and_named(self):
        assert normalise_machine_name("ETHOSCOPE_123") == "ETHOSCOPE_123"
        assert normalise_machine_name("ethoscope dev") == "ETHOSCOPE_DEV"

    @pytest.mark.parametrize("value", [None, "", 7.5, -1, math.inf])
    def test_invalid(self, value):
        assert normalise_machine_name(value) is None


class TestRegionId:
    @pytest.mark.parametrize("value", [3, 3.0, "3", " 3 ", "ROI_3", "roi 3", "tube3"])
    def test_valid(self, value):
        assert normalise_region_id(value) == 3

    @pytest.mark.parametrize("value", [None, "", 0, -2, 2.5, "abc", math.inf])
    def test_invalid(self, value):
        assert normalise_region_id(value) is None


class TestSmallFields:
    def test_sex(self):
        assert [normalise_sex(v) for v in ["M", "male", " F ", "Females", "both"]] == [
            "M",
            "M",
            "F",
            "F",
            "mixed",
        ]
        assert normalise_sex("unknown") is None
        assert normalise_sex(None) is None

    def test_clean_text(self):
        assert clean_text("  CS ") == "CS"
        assert clean_text(1118.0) == "1118"
        assert clean_text(float("nan")) is None

    def test_is_blank(self):
        assert is_blank(pd.NA) and is_blank(None) and is_blank(" ")
        assert not is_blank(0) and not is_blank("x")

    @pytest.mark.parametrize(
        "kernel,stack",
        [
            ("6.1.0-rpi7-rpi-v8", "picamera2"),
            ("6.1.21+rpt-rpi-v7", "picamera2"),
            ("4.19.66-1-ARCH", "legacy"),
            (None, None),
            ("", None),
        ],
    )
    def test_kernel_stack(self, kernel, stack):
        assert kernel_stack(kernel) == stack
