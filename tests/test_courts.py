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
    # for a docket whose CourtListener response lacked court_id.
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


# Pulled from CourtListener API jurisdiction=FB on 2026-05-14, filtered
# to active courts only (defunct/historical entries excluded). Two id
# surprises pinned here: "arb" (not "azb") and "nebraskab" (not "neb").
ALL_BANKRUPTCY_COURTS = frozenset({
    "akb", "almb", "alnb", "alsb", "arb", "areb", "arwb",
    "cacb", "caeb", "canb", "casb", "cob", "ctb",
    "dcb", "deb",
    "flmb", "flnb", "flsb",
    "gamb", "ganb", "gasb", "gub",
    "hib",
    "ianb", "iasb", "idb",
    "ilcb", "ilnb", "ilsb",
    "innb", "insb",
    "ksb",
    "kyeb", "kywb",
    "laeb", "lamb", "lawb",
    "mab", "mdb", "meb",
    "mieb", "miwb",
    "mnb",
    "moeb", "mowb",
    "msnb", "mssb",
    "mtb",
    "nceb", "ncmb", "ncwb",
    "ndb",
    "nebraskab",
    "nhb", "njb", "nmb", "nmib", "nvb",
    "nyeb", "nynb", "nysb", "nywb",
    "ohnb", "ohsb",
    "okeb", "oknb", "okwb",
    "orb",
    "paeb", "pamb", "pawb",
    "prb",
    "rib", "scb", "sdb",
    "tneb", "tnmb", "tnwb",
    "txeb", "txnb", "txsb", "txwb",
    "utb",
    "vaeb", "vawb",
    "vib", "vtb",
    "waeb", "wawb",
    "wieb", "wiwb",
    "wvnb", "wvsb",
    "wyb",
})


def test_bankruptcy_courts_match_parent_district_tz():
    # 28 U.S.C. § 151: a bankruptcy court is a unit of the district court,
    # sharing the district's geographic boundaries — so its tz must match
    # the parent district's tz. CourtListener's ID convention is parent_id[:-1] + 'b'
    # for most courts, with two documented exceptions.
    irregular = {
        "arb": "azd",         # Arizona — CourtListener has no "azb"
        "nebraskab": "ned",   # Nebraska — CourtListener spells the id out in full
    }
    for bk_id in ALL_BANKRUPTCY_COURTS:
        district_id = irregular.get(bk_id, bk_id[:-1] + "d")
        assert district_id in COURT_TIMEZONES, \
            f"bankruptcy {bk_id} expects parent district {district_id} which is missing"
        assert COURT_TIMEZONES[bk_id] == COURT_TIMEZONES[district_id], (
            f"{bk_id} -> {COURT_TIMEZONES[bk_id]} doesn't match parent "
            f"{district_id} -> {COURT_TIMEZONES[district_id]} (per 28 U.S.C. § 151 "
            f"the bankruptcy court is a unit of its district)"
        )


def test_coverage_includes_all_active_bankruptcy_courts():
    assert len(ALL_BANKRUPTCY_COURTS) == 94, \
        f"expected 94 active bankruptcy courts per CourtListener, got {len(ALL_BANKRUPTCY_COURTS)} in fixture"
    missing = ALL_BANKRUPTCY_COURTS - set(COURT_TIMEZONES)
    assert not missing, f"missing bankruptcy courts: {missing}"


def test_defunct_courts_are_not_listed():
    # Defunct/historical court ids were intentionally excluded. Pin a
    # representative sample so a future "add for completeness" doesn't
    # re-introduce them without a separate decision.
    must_not_include = {
        "tennesseeb",     # FB — defunct D. Tennessee, 1797-1801
        "cma",            # MA — renamed to armfor by the 1994 NDAA
        "usafctmilrev", "usarmymilrev", "cgcomilrev", "usnmcmilrev",
    }
    overlap = must_not_include & set(COURT_TIMEZONES)
    assert not overlap, f"defunct ids leaked back into COURT_TIMEZONES: {overlap}"


def test_coverage_includes_all_bankruptcy_appellate_panels():
    # Pulled from CourtListener API jurisdiction=FBP on 2026-05-14.
    must_cover = {"bap1", "bap2", "bap6", "bap8", "bap9", "bap10", "bapma", "bapme"}
    missing = must_cover - set(COURT_TIMEZONES)
    assert not missing, f"missing BAPs: {missing}"
    # The five geographic-circuit BAPs sit in known city principal offices.
    assert tz_for("bap1") == "America/New_York"   # Boston
    assert tz_for("bap6") == "America/New_York"   # Cincinnati
    assert tz_for("bap8") == "America/Chicago"    # St. Louis
    assert tz_for("bap9") == "America/Los_Angeles"  # Pasadena
    assert tz_for("bap10") == "America/Denver"    # Denver


def test_coverage_includes_all_federal_special_tribunals():
    # Pulled from CourtListener API jurisdiction=FS on 2026-05-14.
    # All headquartered in DC / Falls Church VA / Alexandria VA cluster.
    must_cover = {
        "tax", "uscfc", "cit", "cavc", "fisc", "fiscr",
        "asbca", "bia", "bva", "jpml", "mspb", "olc", "ttab",
    }
    missing = must_cover - set(COURT_TIMEZONES)
    assert not missing, f"missing FS tribunals: {missing}"
    for cid in must_cover:
        assert tz_for(cid) == "America/New_York", \
            f"FS tribunal {cid} should be ET (DC-area), got {tz_for(cid)}"


def test_cavc_replaces_dead_vetapp_id():
    # CourtListener has no court with id "vetapp" (returns 404) — the actual id for
    # the Court of Appeals for Veterans Claims is "cavc". Pin this so a
    # future "cleanup" doesn't accidentally re-introduce the dead alias.
    assert "vetapp" not in COURT_TIMEZONES
    assert tz_for("cavc") == "America/New_York"


def test_coverage_includes_all_active_military_appellate_courts():
    # Pulled from CourtListener API jurisdiction=MA on 2026-05-14,
    # restricted to currently-operating courts (the pre-1994 NDAA names
    # cma / usafctmilrev / usarmymilrev / cgcomilrev / usnmcmilrev are
    # excluded — see test_defunct_courts_are_not_listed).
    must_cover = {"armfor", "acca", "afcca", "nmcca", "uscgcoca", "mc"}
    missing = must_cover - set(COURT_TIMEZONES)
    assert not missing, f"missing MA military appellate courts: {missing}"
    for cid in must_cover:
        assert tz_for(cid) == "America/New_York", \
            f"military appellate {cid} should be ET, got {tz_for(cid)}"
