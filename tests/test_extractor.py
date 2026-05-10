from case_calendar.extractor import is_hearing_relevant


def make(desc="", short="", recap_descs=()):
    return {
        "description": desc,
        "short_description": short,
        "recap_documents": [{"description": d} for d in recap_descs],
    }


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
