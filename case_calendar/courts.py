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
    "armfor": ET,                              # Court of Appeals for the Armed Forces
    "vetapp": ET,                              # Court of Appeals for Veterans Claims
    "tax": ET,                                 # US Tax Court
    "uscfc": ET,                               # Court of Federal Claims
    "cit": ET,                                 # Court of International Trade

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

    # ---- Specialty ----
    "fisc": ET,
    "fiscr": ET,
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
