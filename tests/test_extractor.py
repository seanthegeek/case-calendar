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

    def test_appendix_due_is_relevant(self):
        assert is_deadline_relevant(
            make(desc="Joint appendix due 04/14/2026 per local rule")
        )

    def test_rehearing_is_relevant(self):
        # FRAP 40 — petition for panel rehearing / rehearing en banc.
        assert is_deadline_relevant(
            make(desc="Petition for rehearing en banc may be filed within 14 days")
        )

    def test_mandate_is_relevant(self):
        # FRAP 41 — issuance of mandate is how every appeal formally closes.
        assert is_deadline_relevant(make(desc="MANDATE to issue on 06/15/2026"))

    def test_motion_by_party_to_extend_is_relevant(self):
        # Verbatim from 4th Cir docket 25-4607 (Gholinejad).
        assert is_deadline_relevant(
            make(
                desc=(
                    "MOTION by Sina Gholinejad to extend filing time for reply "
                    "brief until June 12, 2026"
                )
            )
        )

    def test_district_court_motion_to_extend_still_matches(self):
        # Regression — bare "motion to extend" still matches after the loosening.
        assert is_deadline_relevant(make(desc="Motion to extend filing deadline"))

    def test_motion_to_extend_does_not_cross_sentence_boundary(self):
        # The {0,80} stretch is bounded by [^.\n] so a "motion" in one
        # sentence can't bleed into an "extend" several sentences later.
        assert not is_deadline_relevant(
            make(desc="Motion was filed yesterday. The court will then need to extend.")
        )

    def test_answer_due_matches(self):
        # FRCP 12(a)(1).
        assert is_deadline_relevant(make(desc="Answer due 06/15/2026"))
        assert is_deadline_relevant(make(desc="ANSWER is due no later than 6/15"))

    def test_disclosures_match(self):
        # FRCP 26(a) initial / expert / pretrial disclosures.
        assert is_deadline_relevant(
            make(desc="Initial disclosures due within 14 days of Rule 26(f) conference")
        )
        assert is_deadline_relevant(make(desc="Expert disclosure deadline 09/01/2026"))

    def test_discovery_cutoff_matches(self):
        # FRCP 16(b)(3)(A) — scheduling order must set discovery cutoff.
        assert is_deadline_relevant(make(desc="Discovery cutoff: 05/01/2026"))
        assert is_deadline_relevant(make(desc="Fact discovery cut-off 05/01/2026"))
        assert is_deadline_relevant(make(desc="Expert discovery closes 06/01/2026"))

    def test_motion_in_limine_matches(self):
        assert is_deadline_relevant(
            make(desc="Motion in limine deadline 14 days before trial")
        )
        assert is_deadline_relevant(make(desc="Motions in limine due 06/15/2026"))

    def test_class_certification_matches(self):
        # FRCP 23(c)(1)(A) — class cert decision "at an early practicable time."
        assert is_deadline_relevant(
            make(desc="Motion for class certification due 06/01/2026")
        )
        assert is_deadline_relevant(make(desc="Class cert briefing schedule"))

    def test_status_report_matches(self):
        assert is_deadline_relevant(make(desc="Joint Status Report due 03/01/2026"))
        assert is_deadline_relevant(make(desc="Status report due in 30 days"))

    def test_mediation_matches(self):
        # ADR under 28 U.S.C. § 651 et seq. and district ADR plans.
        assert is_deadline_relevant(
            make(desc="Mediation deadline 04/15/2026 per ADR L.R. 6")
        )

    def test_mediated_alone_does_not_match(self):
        # `mediation` is a literal word — past-tense `mediated` should not trigger.
        assert not is_deadline_relevant(make(desc="The parties mediated last year"))

    def test_pretrial_order_matches(self):
        # FRCP 16(d) — final pretrial order.
        assert is_deadline_relevant(
            make(desc="Joint pretrial order due 14 days before pretrial conference")
        )

    def test_markman_and_claim_construction_match(self):
        # Patent local rules — claim construction briefing milestones distinct
        # from the Markman hearing itself (which is caught by `hearing`).
        assert is_deadline_relevant(
            make(desc="Opening claim construction brief due 05/01/2026")
        )
        assert is_deadline_relevant(make(desc="Markman briefing schedule entered"))

    def test_presentence_and_psr_match(self):
        # Fed. R. Crim. P. 32 — PSR disclosure / objection / final timing.
        assert is_deadline_relevant(
            make(desc="Presentence Investigation Report due 35 days before sentencing")
        )
        assert is_deadline_relevant(
            make(desc="Objections to PSR due 14 days after disclosure")
        )
        assert is_deadline_relevant(make(desc="Presentencing memorandum filed by USA"))

    def test_presentation_does_not_match_presentence(self):
        # `presentenc\w*` is anchored to the literal stem — words that merely
        # start with "present" must not falsely trigger.
        assert not is_deadline_relevant(make(desc="Oral presentation rescheduled"))

    def test_cipa_matches(self):
        # 18 U.S.C. App. III (Classified Information Procedures Act).
        assert is_deadline_relevant(make(desc="CIPA Section 5 notice due 04/15/2026"))
        assert is_deadline_relevant(
            make(desc="Government's CIPA Section 4 motion filed")
        )

    def test_jencks_matches(self):
        # 18 U.S.C. § 3500; Jencks v. United States, 353 U.S. 657 (1957).
        assert is_deadline_relevant(
            make(desc="Jencks material to be produced 14 days before trial")
        )

    def test_notice_of_appeal_matches(self):
        # Fed. R. App. P. 4(b)(1) — 14-day criminal NoA deadline.
        assert is_deadline_relevant(
            make(desc="Notice of appeal must be filed within 14 days of judgment")
        )

    def test_generic_x_is_due_patterns(self):
        # The "<X> is due" generics catch many criminal-specific deadlines
        # (404(b) notices, Brady/Giglio material, sentencing memoranda, etc.)
        # without needing a dedicated pattern for each.
        assert is_deadline_relevant(
            make(desc="Government's Rule 404(b) notice due 04/15")
        )
        assert is_deadline_relevant(
            make(desc="Sentencing memorandum is due 7 days before sentencing")
        )
        assert is_deadline_relevant(
            make(desc="Brady material is due 30 days before trial")
        )
        assert is_deadline_relevant(make(desc="Expert report due 06/01/2026"))


class TestIsExtractable:
    def test_hearing_only_when_deadlines_off(self):
        # A hearing entry passes regardless of the flag.
        assert is_extractable(
            make(desc="Sentencing set for 4/14/2026"), want_deadlines=False
        )
        assert is_extractable(
            make(desc="Sentencing set for 4/14/2026"), want_deadlines=True
        )

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
            make(recap_descs=("",)),
            want_deadlines=True,
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
        assert is_hearing_relevant(make(short="Plea Agreement Hearing"))

    def test_case_insensitive(self):
        assert is_hearing_relevant(make(desc="SENTENCING"))
        assert is_hearing_relevant(make(desc="sentencing"))

    def test_argued_matches(self):
        # `argu\w*` covers the post-argument docket entry so the verify pass
        # can MARK_HELD the oral-argument hearing row.
        assert is_hearing_relevant(make(desc="Case ARGUED before panel on 6/1/2026"))

    def test_argued_and_submitted_matches(self):
        assert is_hearing_relevant(make(desc="ARGUED AND SUBMITTED"))

    def test_calendared_matches(self):
        assert is_hearing_relevant(
            make(desc="Case calendared for oral argument on 6/1/2026")
        )

    def test_mandate_matches(self):
        assert is_hearing_relevant(make(desc="MANDATE issued 06/15/2026"))

    def test_mandated_does_not_match(self):
        # `mandate` is a literal word, not a stem — "mandated"/"mandates" in
        # ordinary statutory language must not falsely trigger.
        assert not is_hearing_relevant(
            make(desc="The statute mandated quarterly reporting")
        )
