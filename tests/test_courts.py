import logging

from case_calendar.courts import COURT_TIMEZONES, DEFAULT_TZ, tz_for


def test_eastern_courts():
    assert tz_for("nysd") == "America/New_York"
    assert tz_for("flsd") == "America/New_York"
    assert tz_for("dcd") == "America/New_York"
    assert tz_for("cadc") == "America/New_York"
    assert tz_for("mad") == "America/New_York"
    assert tz_for("ded") == "America/New_York"
    assert tz_for("vaed") == "America/New_York"
    assert tz_for("ohnd") == "America/New_York"


def test_central_courts():
    assert tz_for("ilnd") == "America/Chicago"
    assert tz_for("moed") == "America/Chicago"
    assert tz_for("txsd") == "America/Chicago"
    assert tz_for("flnd") == "America/Chicago"  # FL panhandle
    assert tz_for("alnd") == "America/Chicago"
    assert tz_for("tnmd") == "America/Chicago"  # Nashville


def test_mountain_courts():
    assert tz_for("cod") == "America/Denver"
    assert tz_for("nmd") == "America/Denver"
    assert tz_for("utd") == "America/Denver"
    assert tz_for("wyd") == "America/Denver"


def test_pacific_courts():
    assert tz_for("cand") == "America/Los_Angeles"
    assert tz_for("ord") == "America/Los_Angeles"
    assert tz_for("waed") == "America/Los_Angeles"
    assert tz_for("nvd") == "America/Los_Angeles"


def test_no_dst_special_zones():
    # Arizona stays on MST year-round.
    assert tz_for("azd") == "America/Phoenix"
    # Hawaii — note IANA puts Honolulu under Pacific/, not America/.
    assert tz_for("hid") == "Pacific/Honolulu"
    # Puerto Rico.
    assert tz_for("prd") == "America/Puerto_Rico"


def test_alaska_and_pacific_territories():
    assert tz_for("akd") == "America/Anchorage"
    assert tz_for("gud") == "Pacific/Guam"
    assert tz_for("nmid") == "Pacific/Guam"  # Northern Mariana Islands


def test_split_state_assigns_principal_office():
    # Tennessee East = ET, Middle = CT, Western = CT.
    assert tz_for("tned") == "America/New_York"
    assert tz_for("tnmd") == "America/Chicago"
    assert tz_for("tnwd") == "America/Chicago"


def test_circuits_have_correct_tz():
    # All eastern circuits.
    for c in ("ca1", "ca2", "ca3", "ca4", "ca6", "ca11", "cadc", "cafc"):
        assert tz_for(c) == "America/New_York", c
    # Central.
    for c in ("ca5", "ca7", "ca8"):
        assert tz_for(c) == "America/Chicago", c
    assert tz_for("ca9") == "America/Los_Angeles"
    assert tz_for("ca10") == "America/Denver"


def test_unknown_court_falls_back_to_default_with_warning(caplog):
    with caplog.at_level(logging.WARNING):
        tz = tz_for("zzz")
    assert tz == DEFAULT_TZ
    assert any("zzz" in r.message for r in caplog.records), \
        "should warn about the missing court_id"


def test_empty_court_falls_back_silently(caplog):
    # Empty string is a known "unset" sentinel; don't warn on every event
    # for a docket whose CL response lacked court_id.
    with caplog.at_level(logging.WARNING):
        tz = tz_for("")
    assert tz == DEFAULT_TZ
    assert not any(r.levelno == logging.WARNING for r in caplog.records)


def test_all_values_are_valid_iana():
    valid_prefixes = ("America/", "Pacific/")
    for cid, tz in COURT_TIMEZONES.items():
        assert any(tz.startswith(p) for p in valid_prefixes), \
            f"{cid} -> {tz!r} not in expected prefixes"


def test_coverage_includes_all_94_district_court_states():
    # At least one district per state in the union, plus DC and PR.
    must_cover = {
        "alnd", "akd", "azd", "ared", "cand", "cod", "ctd", "ded", "dcd",
        "flsd", "gand", "hid", "idd", "ilnd", "innd", "iand", "ksd",
        "kyed", "laed", "med", "mdd", "mad", "mied", "mnd", "msnd", "moed",
        "mtd", "ned", "nvd", "nhd", "njd", "nmd", "nysd", "nced", "ndd",
        "ohnd", "oked", "ord", "paed", "prd", "rid", "scd", "sdd", "tned",
        "txed", "utd", "vtd", "vaed", "waed", "wvnd", "wied", "wyd",
    }
    missing = must_cover - set(COURT_TIMEZONES)
    assert not missing, f"missing district courts: {missing}"
