from case_calendar.extractor import (
    is_deadline_relevant,
    is_extractable,
    is_hearing_relevant,
)


def make(desc="", short="", recap_descs=()):
    return {
        "description": desc,
        "short_description": short,
        "recap_documents": [{"description": d} for d in recap_descs],
    }


class TestIsDeadlineRelevant:
    def test_empty_entry_is_not_relevant(self):
        assert not is_deadline_relevant(make())

    def test_response_due_is_relevant(self):
        assert is_deadline_relevant(
            make(desc="Response due by 5/24/2026; reply due by 5/31/2026")
        )

    def test_briefing_schedule_order_is_relevant(self):
        assert is_deadline_relevant(
            make(desc="ORDER setting briefing schedule on Motion to Dismiss")
        )

    def test_motion_for_extension_is_relevant(self):
        assert is_deadline_relevant(
            make(desc="MOTION for Extension of Time to File Reply")
        )

    def test_stipulation_is_relevant(self):
        assert is_deadline_relevant(
            make(desc="STIPULATION AND ORDER extending time to respond")
        )

    def test_so_ordered_is_relevant(self):
        assert is_deadline_relevant(
            make(desc="Joint stipulation re briefing schedule. SO ORDERED.")
        )

    def test_brief_filing_is_not_relevant(self):
        assert not is_deadline_relevant(
            make(desc="NOTICE OF ATTORNEY APPEARANCE for USA")
        )


class TestIsExtractable:
    def test_hearing_only_when_deadlines_off(self):
        # A hearing entry passes regardless of the flag.
        assert is_extractable(make(desc="Sentencing set for 4/14/2026"),
                              want_deadlines=False)
        assert is_extractable(make(desc="Sentencing set for 4/14/2026"),
                              want_deadlines=True)

    def test_deadline_blocked_when_deadlines_off(self):
        e = make(desc="Response due by 5/24/2026")
        # Pure deadline language doesn't pass the hearing-only filter.
        assert not is_extractable(e, want_deadlines=False)
        # But does when the case opts in.
        assert is_extractable(e, want_deadlines=True)

    def test_irrelevant_entry_blocked_either_way(self):
        e = make(desc="NOTICE OF ATTORNEY APPEARANCE")
        assert not is_extractable(e, want_deadlines=False)
        assert not is_extractable(e, want_deadlines=True)

    def test_empty_entry_short_circuits(self):
        # Empty entry hits the no-text early return; never touches the regex.
        assert not is_extractable(make(), want_deadlines=False)
        assert not is_extractable(make(), want_deadlines=True)

    def test_recap_doc_with_empty_description_ignored(self):
        # The blob filter in _entry_text drops empty recap-document
        # descriptions rather than dragging them through as " | | ".
        # If they were the ONLY signal carrier and they're empty, the
        # entry is treated as having no text at all.
        assert not is_extractable(
            make(recap_descs=("",)), want_deadlines=True,
        )


class TestIsHearingRelevant:
    def test_empty_entry_is_not_relevant(self):
        assert not is_hearing_relevant(make())

    def test_brief_filing_is_not_relevant(self):
        assert not is_hearing_relevant(
            make(desc="RESPONDENT BRIEF filed by Peter B. Hegseth")
        )

    def test_attorney_appearance_is_not_relevant(self):
        assert not is_hearing_relevant(
            make(desc="NOTICE OF ATTORNEY APPEARANCE for USA")
        )

    def test_sentencing_is_relevant(self):
        assert is_hearing_relevant(make(desc="Sentencing set for 4/14/2026"))

    def test_status_conference_is_relevant(self):
        assert is_hearing_relevant(
            make(desc="Status Conference Reset for 3/2/2026 at 10:30 AM")
        )

    def test_notice_of_rescheduling_is_relevant(self):
        assert is_hearing_relevant(
            make(desc="ELECTRONIC NOTICE OF RESCHEDULING as to OLEKSANDR DIDENKO")
        )

    def test_oral_argument_is_relevant(self):
        assert is_hearing_relevant(make(desc="ORAL ARGUMENT scheduled for 6/1/2026"))

    def test_continued_to_is_relevant(self):
        assert is_hearing_relevant(make(desc="Hearing continued to 5/15/2026"))

    def test_vacated_is_relevant(self):
        assert is_hearing_relevant(make(desc="Sentencing vacated"))

    def test_recap_document_description_can_trigger(self):
        assert is_hearing_relevant(
            make(desc="(blank)", recap_descs=["Notice of Hearing"])
        )

    def test_short_description_can_trigger(self):
        assert is_hearing_relevant(
            make(short="Plea Agreement Hearing")
        )

    def test_case_insensitive(self):
        assert is_hearing_relevant(make(desc="SENTENCING"))
        assert is_hearing_relevant(make(desc="sentencing"))
