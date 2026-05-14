"""Court ID -> IANA timezone mapping.

CourtListener identifies courts with short IDs (e.g. ``flsd``, ``cadc``) and
does not expose a timezone in its API, so we keep the mapping here. Coverage
targets the full set of US federal trial and appellate courts plus the
special-case territories.

Where a court spans multiple timezones (Indiana, Tennessee, Florida
panhandle, Idaho, Kentucky, Kansas, Nebraska, North/South Dakota), we pick
the timezone where the court's principal office sits. Specific divisions
within a district can sit in a different tz; for the calendar use case the
broad-brush mapping is fine — clerks write hearing times in the convening
division's tz, which is what the LLM extracts.

If you hit a court that's missing here, ``tz_for`` logs a warning and
falls back to ``DEFAULT_TZ``. Add the entry rather than living with a
silent default.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Common IANA names reused across many entries.
ET = "America/New_York"
CT = "America/Chicago"
MT = "America/Denver"
PT = "America/Los_Angeles"
ARIZ = "America/Phoenix"          # MST year-round, no DST
AKT = "America/Anchorage"
HST = "Pacific/Honolulu"
PR_TZ = "America/Puerto_Rico"     # AST year-round, no DST
VI_TZ = "America/St_Thomas"       # AST year-round, no DST
GUAM = "Pacific/Guam"             # ChST year-round, no DST
SAMOA = "Pacific/Pago_Pago"       # SST year-round, no DST


COURT_TIMEZONES: dict[str, str] = {
    # ---- US Supreme Court ----
    "scotus": ET,

    # ---- Courts of Appeals ----
    "ca1": ET,    "ca2": ET,    "ca3": ET,    "ca4": ET,
    "ca5": CT,    "ca6": ET,    "ca7": CT,    "ca8": CT,
    "ca9": PT,    "ca10": MT,   "ca11": ET,
    "cadc": ET,   "cafc": ET,

    # ---- District courts: alphabetical by state ----
    # Alabama (CT)
    "alnd": CT, "almd": CT, "alsd": CT,
    # Alaska
    "akd": AKT,
    # Arizona (MST, no DST)
    "azd": ARIZ,
    # Arkansas (CT)
    "ared": CT, "arwd": CT,
    # California (PT)
    "cand": PT, "cacd": PT, "caed": PT, "casd": PT,
    # Colorado (MT)
    "cod": MT,
    # Connecticut (ET)
    "ctd": ET,
    # Delaware (ET)
    "ded": ET,
    # District of Columbia (ET)
    "dcd": ET,
    # Florida — panhandle (flnd) is CT, the rest ET. flnd's principal
    # office is in Pensacola/Tallahassee on CT.
    "flnd": CT, "flmd": ET, "flsd": ET,
    # Georgia (ET)
    "gand": ET, "gamd": ET, "gasd": ET,
    # Hawaii (no DST)
    "hid": HST,
    # Idaho (MT, with the panhandle on PT but principal office is in Boise/MT)
    "idd": MT,
    # Illinois (CT)
    "ilnd": CT, "ilcd": CT, "ilsd": CT,
    # Indiana — most counties ET, a few CT. Both districts principal offices ET.
    "innd": ET, "insd": ET,
    # Iowa (CT)
    "iand": CT, "iasd": CT,
    # Kansas — mostly CT, a few MT counties; principal office is CT.
    "ksd": CT,
    # Kentucky — mostly ET; western counties CT. Principal offices ET.
    "kyed": ET, "kywd": ET,
    # Louisiana (CT)
    "laed": CT, "lamd": CT, "lawd": CT,
    # Maine (ET)
    "med": ET,
    # Maryland (ET)
    "mdd": ET,
    # Massachusetts (ET)
    "mad": ET,
    # Michigan (ET)
    "mied": ET, "miwd": ET,
    # Minnesota (CT)
    "mnd": CT,
    # Mississippi (CT)
    "msnd": CT, "mssd": CT,
    # Missouri (CT)
    "moed": CT, "mowd": CT,
    # Montana (MT)
    "mtd": MT,
    # Nebraska (CT, sliver MT in panhandle)
    "ned": CT,
    # Nevada (PT)
    "nvd": PT,
    # New Hampshire (ET)
    "nhd": ET,
    # New Jersey (ET)
    "njd": ET,
    # New Mexico (MT)
    "nmd": MT,
    # New York (ET)
    "nyed": ET, "nynd": ET, "nysd": ET, "nywd": ET,
    # North Carolina (ET)
    "nced": ET, "ncmd": ET, "ncwd": ET,
    # North Dakota — mostly CT, sliver MT. Principal CT.
    "ndd": CT,
    # Ohio (ET)
    "ohnd": ET, "ohsd": ET,
    # Oklahoma (CT)
    "oked": CT, "oknd": CT, "okwd": CT,
    # Oregon (PT)
    "ord": PT,
    # Pennsylvania (ET)
    "paed": ET, "pamd": ET, "pawd": ET,
    # Puerto Rico (no DST)
    "prd": PR_TZ,
    # Rhode Island (ET)
    "rid": ET,
    # South Carolina (ET)
    "scd": ET,
    # South Dakota — mostly CT, sliver MT. Principal CT.
    "sdd": CT,
    # Tennessee — east TN is ET, middle/west TN is CT. Three districts:
    "tned": ET,    # Eastern (Knoxville/Chattanooga)
    "tnmd": CT,    # Middle (Nashville)
    "tnwd": CT,    # Western (Memphis)
    # Texas — mostly CT (El Paso division of txwd is MT but principal CT).
    "txed": CT, "txnd": CT, "txsd": CT, "txwd": CT,
    # Utah (MT)
    "utd": MT,
    # Vermont (ET)
    "vtd": ET,
    # Virginia (ET)
    "vaed": ET, "vawd": ET,
    # Washington state (PT)
    "waed": PT, "wawd": PT,
    # West Virginia (ET)
    "wvnd": ET, "wvsd": ET,
    # Wisconsin (CT)
    "wied": CT, "wiwd": CT,
    # Wyoming (MT)
    "wyd": MT,
    # Territories
    "vid": VI_TZ,                              # US Virgin Islands
    "gud": GUAM,                               # Guam
    "nmid": GUAM,                              # Northern Mariana Islands
    "asd": SAMOA,                              # American Samoa

    # ---- Bankruptcy courts ----
    # 95 federal bankruptcy courts as enumerated by CourtListener under
    # jurisdiction=FB (fetched 2026-05-14). A bankruptcy court is a "unit
    # of the district court" under 28 U.S.C. § 151, sharing the parent
    # district's geographic boundaries — so each bankruptcy court's
    # timezone matches its parent district's (above), and the same
    # principal-office tiebreakers used for multi-tz districts apply.
    #
    # ID-naming surprises worth pinning down: Arizona is "arb" (NOT
    # "azb"), and Nebraska is "nebraskab" (NOT "neb" — that ID would
    # collide with the Nebraska district "ned" suffix conventions).
    #
    # Alabama (CT)
    "alnb": CT, "almb": CT, "alsb": CT,
    # Alaska
    "akb": AKT,
    # Arizona (MST, no DST) — CL id is "arb", not "azb".
    "arb": ARIZ,
    # Arkansas (CT)
    "areb": CT, "arwb": CT,
    # California (PT)
    "canb": PT, "cacb": PT, "caeb": PT, "casb": PT,
    # Colorado (MT)
    "cob": MT,
    # Connecticut (ET)
    "ctb": ET,
    # Delaware (ET)
    "deb": ET,
    # District of Columbia (ET)
    "dcb": ET,
    # Florida — flnb principal office Tallahassee (CT panhandle); flmb/flsb ET.
    "flnb": CT, "flmb": ET, "flsb": ET,
    # Georgia (ET)
    "ganb": ET, "gamb": ET, "gasb": ET,
    # Hawaii (no DST)
    "hib": HST,
    # Idaho (MT, panhandle PT — principal office in Boise/MT)
    "idb": MT,
    # Illinois (CT)
    "ilnb": CT, "ilcb": CT, "ilsb": CT,
    # Indiana (both districts principal offices ET)
    "innb": ET, "insb": ET,
    # Iowa (CT)
    "ianb": CT, "iasb": CT,
    # Kansas (CT)
    "ksb": CT,
    # Kentucky (ET)
    "kyeb": ET, "kywb": ET,
    # Louisiana (CT)
    "laeb": CT, "lamb": CT, "lawb": CT,
    # Maine (ET)
    "meb": ET,
    # Maryland (ET)
    "mdb": ET,
    # Massachusetts (ET)
    "mab": ET,
    # Michigan (ET)
    "mieb": ET, "miwb": ET,
    # Minnesota (CT)
    "mnb": CT,
    # Mississippi (CT)
    "msnb": CT, "mssb": CT,
    # Missouri (CT)
    "moeb": CT, "mowb": CT,
    # Montana (MT)
    "mtb": MT,
    # Nebraska (CT) — CL id is "nebraskab", not "neb".
    "nebraskab": CT,
    # Nevada (PT)
    "nvb": PT,
    # New Hampshire (ET)
    "nhb": ET,
    # New Jersey (ET)
    "njb": ET,
    # New Mexico (MT)
    "nmb": MT,
    # New York (ET)
    "nyeb": ET, "nynb": ET, "nysb": ET, "nywb": ET,
    # North Carolina (ET)
    "nceb": ET, "ncmb": ET, "ncwb": ET,
    # North Dakota (CT — principal office Fargo/Bismarck)
    "ndb": CT,
    # Ohio (ET)
    "ohnb": ET, "ohsb": ET,
    # Oklahoma (CT)
    "okeb": CT, "oknb": CT, "okwb": CT,
    # Oregon (PT)
    "orb": PT,
    # Pennsylvania (ET)
    "paeb": ET, "pamb": ET, "pawb": ET,
    # Puerto Rico (no DST)
    "prb": PR_TZ,
    # Rhode Island (ET)
    "rib": ET,
    # South Carolina (ET)
    "scb": ET,
    # South Dakota (CT)
    "sdb": CT,
    # Tennessee — east TN ET, middle/west TN CT.
    "tneb": ET, "tnmb": CT, "tnwb": CT,
    # Texas (CT — txwb's El Paso division is MT but principal office is CT)
    "txeb": CT, "txnb": CT, "txsb": CT, "txwb": CT,
    # Utah (MT)
    "utb": MT,
    # Vermont (ET)
    "vtb": ET,
    # Virginia (ET)
    "vaeb": ET, "vawb": ET,
    # Washington state (PT)
    "waeb": PT, "wawb": PT,
    # West Virginia (ET)
    "wvnb": ET, "wvsb": ET,
    # Wisconsin (CT)
    "wieb": CT, "wiwb": CT,
    # Wyoming (MT)
    "wyb": MT,
    # Bankruptcy court territories
    "gub": GUAM,
    "nmib": GUAM,
    "vib": VI_TZ,

    # ---- Bankruptcy Appellate Panels ----
    # 8 active BAPs as enumerated by CourtListener under jurisdiction=FBP
    # (fetched 2026-05-14). BAPs sit on appeals from bankruptcy courts in
    # their circuit; the timezone reflects the panel clerk's principal
    # office, since panel sessions rotate among member districts.
    "bap1": ET,    # First Circuit — clerk's office Boston, MA
    "bap2": ET,    # Second Circuit — NYC
    "bap6": ET,    # Sixth Circuit — Cincinnati, OH
    "bap8": CT,    # Eighth Circuit — St. Louis, MO
    "bap9": PT,    # Ninth Circuit — Pasadena, CA
    "bap10": MT,   # Tenth Circuit — Denver, CO
    # Sub-panel ids used by the First Circuit BAP when sitting in a
    # specific member district.
    "bapma": ET,   # BAP sitting in Massachusetts
    "bapme": ET,   # BAP sitting in D. Maine

    # ---- Specialty federal courts and administrative tribunals ----
    # CL jurisdiction=FS (Federal Special). All are headquartered in
    # Washington, DC or the nearby Falls Church / Alexandria, VA cluster
    # (ET). Sourced from CL's /courts/?jurisdiction=FS listing fetched
    # 2026-05-14.
    "tax": ET,        # US Tax Court (Washington, DC)
    "uscfc": ET,      # US Court of Federal Claims (Washington, DC)
    "cit": ET,        # US Court of International Trade (NYC)
    "cavc": ET,       # US Court of Appeals for Veterans Claims (DC).
                      # Note: CL's id is `cavc`, NOT `vetapp` (404 in CL).
    "fisc": ET,       # Foreign Intelligence Surveillance Court (DC)
    "fiscr": ET,      # FISC of Review (DC)
    "asbca": ET,      # Armed Services Board of Contract Appeals (Falls Church, VA)
    "bia": ET,        # Board of Immigration Appeals (Falls Church, VA)
    "bva": ET,        # Board of Veterans' Appeals (Washington, DC)
    "jpml": ET,       # Judicial Panel on Multidistrict Litigation (Washington, DC)
    "mspb": ET,       # Merit Systems Protection Board (Washington, DC)
    "olc": ET,        # DOJ Office of Legal Counsel (Washington, DC) — not
                      # a court but CL lists it under FS for opinion publishing
    "ttab": ET,       # USPTO Trademark Trial and Appeal Board (Alexandria, VA)

    # ---- Military appellate ----
    # CL jurisdiction=MA. All ET — the Courts of Criminal Appeals sit at
    # their respective service installations in the DC / MD / VA
    # cluster, and the Court of Appeals for the Armed Forces (USCAAF)
    # sits in Washington, DC.
    "armfor": ET,         # US Court of Appeals for the Armed Forces (DC)
    "acca": ET,           # Army Court of Criminal Appeals (Fort Belvoir, VA)
    "afcca": ET,          # Air Force Court of Criminal Appeals (Joint Base Andrews, MD)
    "nmcca": ET,          # Navy-Marine Corps Court of Criminal Appeals (Washington Navy Yard, DC)
    "uscgcoca": ET,       # Coast Guard Court of Criminal Appeals (Washington, DC)
    "mc": ET,             # Court of Military Commission Review (Washington, DC)
}

DEFAULT_TZ = ET


def tz_for(court_id: str) -> str:
    if not court_id:
        return DEFAULT_TZ
    tz = COURT_TIMEZONES.get(court_id)
    if tz is None:
        log.warning(
            "courts: no timezone mapping for court_id=%r; falling back to %s. "
            "Add an entry to COURT_TIMEZONES in case_calendar/courts.py.",
            court_id, DEFAULT_TZ,
        )
        return DEFAULT_TZ
    return tz
