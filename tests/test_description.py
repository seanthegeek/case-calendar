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


def test_docket_entry_numbers_render_above_cl_ids():
    # Subscribers see PACER positions ("[65]") in the CL UI; surfacing them
    # in the description spares a lookup. The line lives directly above the
    # CL entry IDs so the audit trail reads docket-first, ID-second.
    out = build(
        notes=None, dial_in=None, docket_number="1:25-cr-1",
        court_citation="D. Mass.", docket_absolute_url="/d/1/",
        source_entry_ids=[1001, 1002],
        docket_entry_numbers=[65, 66],
    )
    sections = out.split("\n\n")
    docket_idx = next(i for i, s in enumerate(sections)
                      if s.startswith("Docket entries:"))
    cl_idx = next(i for i, s in enumerate(sections)
                  if s.startswith("CourtListener entry IDs:"))
    assert docket_idx < cl_idx
    assert "Docket entries: 65, 66" in out


def test_docket_entry_numbers_omitted_when_none_known():
    # All source entries lacked a docket position (paperless minute orders);
    # the line is skipped entirely rather than rendering an empty list.
    out = build(
        notes=None, dial_in=None, docket_number=None, court_citation=None,
        docket_absolute_url=None,
        source_entry_ids=[1001],
        docket_entry_numbers=[],
    )
    assert "Docket entries:" not in out
    assert "CourtListener entry IDs: 1001" in out


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
