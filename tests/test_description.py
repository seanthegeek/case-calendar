from case_calendar.calendars.description import build


def test_minimal_description_is_empty():
    assert build(
        notes=None, dial_in=None, docket_number=None, court_citation=None,
        docket_absolute_url=None, source_entry_ids=None,
    ) == ""


def test_single_judge_renders_as_judge_label():
    out = build(
        notes=None, dial_in=None, docket_number=None, court_citation=None,
        docket_absolute_url=None, source_entry_ids=None,
        judge="Hon. Jane Smith",
    )
    assert "Judge: Hon. Jane Smith" in out
    assert "Panel:" not in out


def test_appellate_panel_renders_as_panel_label():
    # Comma in the judge string => appellate panel; render with "Panel:".
    out = build(
        notes=None, dial_in=None, docket_number=None, court_citation=None,
        docket_absolute_url=None, source_entry_ids=None,
        judge="Henderson, Katsas, Rao",
    )
    assert "Panel: Henderson, Katsas, Rao" in out
    assert "Judge: Henderson" not in out


def test_full_description_renders_in_order():
    out = build(
        notes="Sentencing.",
        dial_in="https://meet.example/abc",
        docket_number="1:25-cr-10273-NMG",
        court_citation="D. Mass.",
        docket_absolute_url="/docket/70678228/foo/",
        source_entry_ids=[35, 31],
    )
    sections = out.split("\n\n")
    assert sections[0] == "Sentencing."
    assert sections[1].startswith("Dial-in / link:")
    assert "Case: 1:25-cr-10273-NMG (D. Mass.)" in out
    assert "Docket: https://www.courtlistener.com/docket/70678228/foo/" in out
    assert "CourtListener entry IDs: 35, 31" in out


def test_absolute_url_kept_intact_when_already_full():
    out = build(
        notes=None, dial_in=None, docket_number=None, court_citation=None,
        docket_absolute_url="https://www.courtlistener.com/docket/9/x/",
        source_entry_ids=None,
    )
    assert "Docket: https://www.courtlistener.com/docket/9/x/" in out
    # No double-prefix.
    assert "https://www.courtlistener.comhttps" not in out


def test_no_source_text_block_emitted():
    # Description shows only the structured fields plus the entry-id audit
    # line. The raw docket prose lives one click away at the Docket: URL.
    out = build(
        notes="Short note.", dial_in=None, docket_number="1:25-cv-1",
        court_citation="N.D. Cal.", docket_absolute_url="/docket/1/foo/",
        source_entry_ids=[35, 31],
    )
    assert "Source text:" not in out
    assert "Docket entry " not in out
    assert "PDF excerpt" not in out
    assert "CourtListener entry IDs: 35, 31" in out
