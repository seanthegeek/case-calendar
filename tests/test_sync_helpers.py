"""Pure-function unit tests for sync.py."""

from case_calendar.store import compact_recap_documents
from case_calendar.sync import (
    _append_audit_line,
    _deadline_local_to_utc,
    _default_duration,
    _docket_implies_deadlines,
    _extract_docket_refs,
    _is_fetchable,
    _local_to_utc,
    _mark_held_date_matches,
    _needs_pdf,
    _validate_action_dial_in,
    fingerprint_entry,
)


# --- _is_fetchable ---


class TestIsFetchable:
    def test_paperless_placeholder(self):
        assert not _is_fetchable(
            {
                "is_available": False,
                "is_sealed": None,
                "filepath_local": None,
                "filepath_ia": "",
                "plain_text": "",
            }
        )

    def test_sealed(self):
        assert not _is_fetchable(
            {
                "is_available": False,
                "is_sealed": True,
                "filepath_local": None,
                "filepath_ia": "",
                "plain_text": "",
            }
        )

    def test_extracted_text_is_fetchable_even_if_not_marked_available(self):
        # If CourtListener already gave us the text, that's all we need.
        assert _is_fetchable(
            {
                "is_available": False,
                "plain_text": "the document body",
                "filepath_local": None,
                "filepath_ia": "",
            }
        )

    def test_available_with_filepath_local(self):
        assert _is_fetchable(
            {
                "is_available": True,
                "filepath_local": "recap/foo.pdf",
                "filepath_ia": "",
                "plain_text": "",
            }
        )

    def test_available_with_filepath_ia(self):
        assert _is_fetchable(
            {
                "is_available": True,
                "filepath_local": None,
                "filepath_ia": "https://archive.org/.../foo.pdf",
                "plain_text": "",
            }
        )

    def test_available_but_no_paths_is_not_fetchable(self):
        assert not _is_fetchable(
            {
                "is_available": True,
                "filepath_local": None,
                "filepath_ia": "",
                "plain_text": "",
            }
        )


# --- _needs_pdf ---


def _entry(desc):
    return {"description": desc, "short_description": ""}


class TestNeedsPdf:
    def test_with_specific_time_no_pdf_needed(self):
        assert not _needs_pdf(_entry("Sentencing set for 4/14/2026 03:00 PM"))

    def test_with_courtroom_no_pdf_needed(self):
        assert not _needs_pdf(_entry("Hearing in Courtroom 4 before Judge X"))

    def test_with_zoom_link_no_pdf_needed(self):
        assert not _needs_pdf(_entry("Hearing held via zoom"))

    def test_sparse_description_needs_pdf(self):
        assert _needs_pdf(_entry("Notice of Hearing"))

    def test_empty_description_needs_pdf(self):
        assert _needs_pdf(_entry(""))

    def test_entered_footer_does_not_satisfy_hint(self):
        # CourtListener appends "[Entered: MM/DD/YYYY HH:MM AM/PM]" to almost every entry;
        # without stripping it, the time-of-day match fools _needs_pdf into
        # skipping the PDF that holds the actual hearing time.
        assert _needs_pdf(
            _entry(
                "PER CURIAM ORDER allocating oral argument time. "
                "[Entered: 05/06/2026 01:51 PM]"
            )
        )

    def test_entered_paren_form_also_stripped(self):
        assert _needs_pdf(_entry("Notice of Hearing (Entered: 03/13/2026 11:30 AM)"))

    def test_real_inline_time_still_satisfies_hint(self):
        # Backstop: if the time really is in the description body (not just
        # the clerk footer), we still skip the PDF.
        assert not _needs_pdf(
            _entry(
                "Sentencing set for 4/14/2026 03:00 PM. [Entered: 04/01/2026 09:00 AM]"
            )
        )

    def test_order_granting_motion_for_hearing_forces_pdf(self):
        # Even with an inline time, an order granting a Motion for Hearing
        # references the underlying motion only by docket position; the PDF
        # carries the full ruling and any CIPA / scheduling details that
        # weren't echoed in the brief description.
        assert _needs_pdf(
            _entry(
                "ORDER granting 65 Motion for Hearing as to Ashtor. "
                "Calendar Call set for 6/10/2026 at 9:30 AM."
            )
        )

    def test_order_granting_motion_to_continue_forces_pdf(self):
        assert _needs_pdf(
            _entry(
                "ORDER granting 42 Motion to Continue Trial. "
                "Trial reset for 8/15/2026 at 10:00 AM."
            )
        )

    def test_order_granting_motion_for_continuance_forces_pdf(self):
        assert _needs_pdf(
            _entry(
                "ORDER granting Defendant's Motion for Continuance. "
                "New trial date 9/1/2026 at 9:00 AM."
            )
        )

    def test_order_granting_substantive_motion_does_not_force_pdf(self):
        # Substantive rulings don't move the docket; existing _DETAIL_HINTS
        # logic governs PDF fetch as before.
        assert not _needs_pdf(
            _entry(
                "ORDER granting 50 Motion to Suppress. "
                "Hearing concluded 4/14/2026 03:00 PM."
            )
        )


# --- _extract_docket_refs ---


class TestExtractDocketRefs:
    def test_grants_numbered_motion(self):
        refs = _extract_docket_refs(
            _entry("ORDER granting 65 Motion for Hearing as to Ashtor.")
        )
        assert refs == [65]

    def test_grants_in_part(self):
        refs = _extract_docket_refs(
            _entry("ORDER granting in part 100 Motion to Compel.")
        )
        assert refs == [100]

    def test_bracketed_form(self):
        refs = _extract_docket_refs(
            _entry("Response to [42] Motion to Dismiss filed by Plaintiff.")
        )
        assert refs == [42]

    def test_dedupes(self):
        refs = _extract_docket_refs(
            _entry("ORDER granting 65 Motion for Hearing. See 65 for full text.")
        )
        assert refs == [65]

    def test_entered_footer_date_not_picked_up(self):
        # The "(Entered: 12/26/2025)" footer has digits that look like docket
        # numbers; stripping the footer first prevents false matches.
        refs = _extract_docket_refs(
            _entry("ORDER granting 65 Motion. (Entered: 12/26/2025)")
        )
        assert refs == [65]

    def test_no_refs_for_normal_entry(self):
        assert _extract_docket_refs(_entry("Notice of Hearing")) == []


# --- _local_to_utc ---


class TestLocalToUtc:
    def test_naive_local_time_with_eastern(self):
        # 3pm EST in January = 20:00 UTC
        assert (
            _local_to_utc("2026-01-07", "15:00", "America/New_York")
            == "2026-01-07T20:00:00+00:00"
        )

    def test_eastern_summer_offset(self):
        # 3pm EDT in April = 19:00 UTC
        assert (
            _local_to_utc("2026-04-14", "15:00", "America/New_York")
            == "2026-04-14T19:00:00+00:00"
        )

    def test_pacific_time(self):
        assert (
            _local_to_utc("2026-04-14", "10:00", "America/Los_Angeles")
            == "2026-04-14T17:00:00+00:00"
        )

    def test_date_only_treated_as_midnight_local(self):
        out = _local_to_utc("2026-04-14", None, "America/New_York")
        assert out == "2026-04-14T04:00:00+00:00"

    def test_empty_date_returns_none(self):
        assert _local_to_utc("", None, "America/New_York") is None


# --- _default_duration ---


class TestDefaultDuration:
    def test_known_types(self):
        assert _default_duration("sentencing", time_set=True) == 90
        assert _default_duration("trial", time_set=True) == 240
        assert _default_duration("oral_argument", time_set=True) == 60
        assert _default_duration("status_conference", time_set=True) == 30

    def test_unknown_type_falls_back_to_60(self):
        assert _default_duration("weird_thing", time_set=True) == 60
        assert _default_duration(None, time_set=True) == 60

    def test_no_time_means_all_day(self):
        assert _default_duration("sentencing", time_set=False) == 0


# --- fingerprint_entry ---


class TestFingerprint:
    def test_stable_for_identical_input(self):
        e = {
            "id": 1,
            "description": "x",
            "short_description": "",
            "date_filed": "2026-01-01",
            "recap_documents": [],
        }
        assert fingerprint_entry(e) == fingerprint_entry(dict(e))

    def test_changes_when_description_changes(self):
        a = {
            "description": "x",
            "short_description": "",
            "date_filed": "d",
            "recap_documents": [],
        }
        b = dict(a, description="y")
        assert fingerprint_entry(a) != fingerprint_entry(b)

    def test_changes_when_pdf_becomes_available(self):
        before = {
            "description": "Notice of Hearing",
            "short_description": "",
            "date_filed": "d",
            "recap_documents": [
                {
                    "description": "Notice",
                    "is_available": False,
                    "is_sealed": None,
                    "plain_text": "",
                }
            ],
        }
        after = dict(before)
        after["recap_documents"] = [
            {
                "description": "Notice",
                "is_available": True,
                "is_sealed": None,
                "plain_text": "the body",
            }
        ]
        assert fingerprint_entry(before) != fingerprint_entry(after), (
            "PDF availability flip should re-trigger processing"
        )

    def test_changes_when_text_becomes_available(self):
        before = {
            "description": "x",
            "short_description": "",
            "date_filed": "d",
            "recap_documents": [
                {
                    "description": "doc",
                    "is_available": True,
                    "is_sealed": None,
                    "plain_text": "",
                }
            ],
        }
        after = dict(before)
        after["recap_documents"] = [
            {
                "description": "doc",
                "is_available": True,
                "is_sealed": None,
                "plain_text": "now we have OCR",
            }
        ]
        assert fingerprint_entry(before) != fingerprint_entry(after)


class TestDocketImpliesDeadlines:
    def test_civil_district_court(self):
        assert _docket_implies_deadlines("1:23-cv-04567-AB") is True

    def test_criminal_felony(self):
        assert _docket_implies_deadlines("1:25-cr-00001-XY-1") is False

    def test_criminal_misdemeanor(self):
        assert _docket_implies_deadlines("2:24-cm-00099") is False

    def test_appellate_no_type_code_defaults_on(self):
        # Circuit court numbering omits the cv/cr split — appellate briefing
        # schedules ARE worth tracking, so unknown defaults to True.
        assert _docket_implies_deadlines("23-1234") is True

    def test_specialty_court_defaults_on(self):
        # Tax / Federal Claims / CIT — civil-flavored, deadlines on.
        assert _docket_implies_deadlines("12345-22") is True

    def test_missing_returns_none(self):
        assert _docket_implies_deadlines(None) is None
        assert _docket_implies_deadlines("") is None


# --- _validate_action_dial_in ---


class TestValidateActionDialIn:
    def test_no_dial_in_is_noop(self, monkeypatch):
        from case_calendar import url_validator

        called: list[str] = []

        def _fake(url, **kw):
            called.append(url)

        monkeypatch.setattr(url_validator, "validate_url", _fake)
        action = {"type": "ADD"}
        _validate_action_dial_in(action)
        assert called == []
        assert "dial_in" not in action

    def test_valid_url_unchanged(self, monkeypatch):
        from case_calendar import url_validator

        monkeypatch.setattr(
            url_validator,
            "validate_url",
            lambda u, **kw: u,  # passes through unchanged
        )
        action = {"dial_in": "https://zoom.us/j/123"}
        _validate_action_dial_in(action)
        assert action["dial_in"] == "https://zoom.us/j/123"
        assert "notes" not in action

    def test_repaired_url_replaces_original(self, monkeypatch):
        from case_calendar import url_validator

        monkeypatch.setattr(
            url_validator,
            "validate_url",
            lambda u, **kw: "https://zoom.us/j/123/",  # parent-path repair
        )
        action = {"dial_in": "https://zoom.us/j/123/junk"}
        _validate_action_dial_in(action)
        assert action["dial_in"] == "https://zoom.us/j/123/"

    def test_invalid_url_moved_to_notes(self, monkeypatch):
        from case_calendar import url_validator

        monkeypatch.setattr(
            url_validator,
            "validate_url",
            lambda u, **kw: None,
        )
        action = {"dial_in": "https://broken.example.com/x"}
        _validate_action_dial_in(action)
        assert action["dial_in"] is None
        assert "Dial-in (unverified)" in action["notes"]
        assert "broken.example.com" in action["notes"]

    def test_invalid_url_appends_to_existing_notes(self, monkeypatch):
        from case_calendar import url_validator

        monkeypatch.setattr(
            url_validator,
            "validate_url",
            lambda u, **kw: None,
        )
        action = {
            "dial_in": "https://broken.example.com/x",
            "notes": "Existing notes line.",
        }
        _validate_action_dial_in(action)
        assert action["notes"].startswith("Existing notes line.")
        assert "Dial-in (unverified)" in action["notes"]


# --- _mark_held_date_matches ---


class TestMarkHeldDateMatches:
    def test_no_action_date_returns_true(self):
        assert _mark_held_date_matches(
            {},
            {"starts_at_utc": "2026-04-14T15:00:00+00:00"},
        )

    def test_no_existing_starts_returns_true(self):
        assert _mark_held_date_matches({"local_date": "2026-04-14"}, {})

    def test_same_date_returns_true(self):
        assert _mark_held_date_matches(
            {"local_date": "2026-04-14"},
            {"starts_at_utc": "2026-04-14T15:00:00+00:00"},
        )

    def test_within_two_days_returns_true(self):
        assert _mark_held_date_matches(
            {"local_date": "2026-04-15"},  # +1 day
            {"starts_at_utc": "2026-04-14T15:00:00+00:00"},
        )

    def test_outside_window_returns_false(self):
        assert not _mark_held_date_matches(
            {"local_date": "2026-04-20"},
            {"starts_at_utc": "2026-04-14T15:00:00+00:00"},
        )

    def test_malformed_dates_fall_open_to_true(self):
        # Garbage in either date -> can't compare; treat as matching so
        # the action proceeds (consistent with the function's docstring).
        assert _mark_held_date_matches(
            {"local_date": "not-a-date"},
            {"starts_at_utc": "2026-04-14T15:00:00+00:00"},
        )


# --- _deadline_local_to_utc ---


class TestDeadlineLocalToUtc:
    def test_explicit_time_used_as_is(self):
        # Real time supplied → no 17:00 default.
        out = _deadline_local_to_utc("2026-05-24", "09:00", "America/New_York")
        assert out == "2026-05-24T13:00:00+00:00"  # 9am EDT == 13:00 UTC

    def test_missing_time_defaults_to_5pm(self):
        out = _deadline_local_to_utc("2026-05-24", None, "America/New_York")
        # 5pm EDT (DST in May) = 21:00 UTC.
        assert out == "2026-05-24T21:00:00+00:00"

    def test_empty_date_returns_none(self):
        assert _deadline_local_to_utc("", None, "America/New_York") is None


# --- compact_recap_documents ---


class TestCompactRecapDocuments:
    def test_orders_main_doc_before_attachments(self):
        entry = {
            "recap_documents": [
                {"id": 102, "document_number": 65, "attachment_number": 2},
                {"id": 100, "document_number": 65, "attachment_number": None},
                {"id": 101, "document_number": 65, "attachment_number": 1},
            ]
        }
        out = compact_recap_documents(entry)
        assert [d["id"] for d in out] == [100, 101, 102]

    def test_handles_non_integer_position_fields(self):
        # Non-integer document_number / attachment_number coerce to 0 so the
        # sort doesn't crash; the rows still appear, just at the head.
        entry = {
            "recap_documents": [
                {"id": 1, "document_number": "x", "attachment_number": None},
                {"id": 2, "document_number": "y", "attachment_number": "z"},
            ]
        }
        out = compact_recap_documents(entry)
        assert len(out) == 2

    def test_empty_input_empty_output(self):
        assert compact_recap_documents({"recap_documents": []}) == []
        assert compact_recap_documents({}) == []


class TestAppendAuditLine:
    def test_no_existing_audit_returns_just_the_line(self):
        assert _append_audit_line(None, "verify-pass", "note") == "[verify-pass] note"
        assert _append_audit_line("", "dedupe", "x") == "[dedupe] x"

    def test_existing_audit_appends_with_blank_line_separator(self):
        # Audit paragraphs stack across sync runs, separated by a blank line
        # so the column stays readable.
        out = _append_audit_line("[earlier] prior note", "verify-pass", "next")
        assert out == "[earlier] prior note\n\n[verify-pass] next"

    def test_existing_audit_trailing_newlines_are_trimmed_before_append(self):
        out = _append_audit_line("[earlier] prior\n\n", "dedupe", "merged")
        assert out == "[earlier] prior\n\n[dedupe] merged"
