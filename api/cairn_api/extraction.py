"""Free-text intake: pick the spec's data_fields out of the user's own words (UC-CASE-01).

A rule-based stand-in until a model does this. It proposes values for the
data_fields keys only, and each proposal is checked against the same types as
a button answer. Nothing is saved here. The user sees a readback and confirms
it ("Did I get that right?"). Anything that isn't a data_fields value is
discarded, including any cause of death: circumstance is proposed only from
general words (ill, hospice, sudden, accident, investigation), never from a
medical cause, and its own words are never kept.

The text reaching this module has already been through redaction.redact.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from pydantic import TypeAdapter, ValidationError

from .schemas import FIELD_VALUE_TYPES, US_STATE_CODES, FieldKey

STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC", "washington dc": "DC", "puerto rico": "PR",
    "guam": "GU", "us virgin islands": "VI", "american samoa": "AS", "northern mariana islands": "MP",
}
# Longest names first, so "west virginia" wins over "virginia".
_STATE_NAME = re.compile(r"\b(" + "|".join(sorted(map(re.escape, STATE_NAMES), key=len, reverse=True)) + r")\b",
                         re.IGNORECASE)
_CITY_STATE = re.compile(r"\b([A-Z][a-zA-Z.'-]+(?: [A-Z][a-zA-Z.'-]+){0,2}), ([A-Z]{2})\b")
_COUNTY = re.compile(r"\b([A-Z][a-zA-Z.'-]+(?: [A-Z][a-zA-Z.'-]+)? County)\b")
_ABROAD = re.compile(
    r"\b(abroad|overseas|out of the country|outside (of )?the (us|u\.s\.|united states|country)|"
    r"in (canada|mexico|england|scotland|ireland|france|germany|italy|spain|portugal|india|china|japan|"
    r"the philippines|vietnam|israel|jamaica|the dominican republic|brazil|colombia|australia|greece))\b",
    re.IGNORECASE,
)
_AWAY = re.compile(r"\b(away from home|while (travel+ing|visiting|on vacation)|on (a )?(vacation|trip)|"
                   r"visiting (family|friends|us|my \w+)|traveling)\b", re.IGNORECASE)
_LIVED_IN = re.compile(r"\b(lived|lives|living|home (is|was)|resided)\b[^.]{0,40}?\bin\b", re.IGNORECASE)

_DEATH_WORDS = re.compile(r"\b(died|dies|passed|passing|death|lost (him|her|them)|gone|pronounced)\b", re.IGNORECASE)
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"])}
_MONTH_DATE = re.compile(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b",
                         re.IGNORECASE)
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_TODAY = re.compile(r"\b(today|this morning|this afternoon|this evening|tonight|earlier today|an hour ago|"
                    r"a few hours ago)\b", re.IGNORECASE)
_YESTERDAY = re.compile(r"\b(yesterday|last night)\b", re.IGNORECASE)
_THIS_WEEK = re.compile(r"\b(this week|earlier this week|a few days ago|a couple (of )?days ago|two days ago|"
                        r"three days ago|on (monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b",
                        re.IGNORECASE)
_DATE_UNKNOWN = re.compile(r"\b(don'?t know when|not sure when|no idea when)\b", re.IGNORECASE)


def _who_died(words: str) -> re.Pattern:
    """ "my mom died" or "we lost our son", not "while visiting my sister"."""
    return re.compile(rf"\b(?:(?:my|our) (?:{words})\b(?=[^.]{{0,60}}?\b(?:died|passed|is gone|was pronounced))|"
                      rf"lost (?:my|our) (?:{words})\b)", re.IGNORECASE)


_ROLES = [
    (_who_died(r"husband|wife|partner|spouse|fianc[eé]e?"), "spouse_partner", None),
    (_who_died(r"mom|mother|mum|mommy"), "child", "Mom"),
    (_who_died(r"dad|father|daddy"), "child", "Dad"),
    (_who_died(r"grandma|grandmother|nana"), "other_family", "Grandma"),
    (_who_died(r"grandpa|grandfather"), "other_family", "Grandpa"),
    (_who_died(r"brother|sister|son|daughter|aunt|uncle|cousin|niece|nephew|stepmother|stepfather"),
     "other_family", None),
    (_who_died(r"best friend|friend"), "friend", None),
    (re.compile(r"\b(i'?m|i am) (the )?(named )?executor\b", re.I), "named_executor", None),
    (re.compile(r"\b(i (had|have|held|was) (the |their |his |her )?power of attorney|"
                r"i was (the |their |his |her )?poa)\b", re.I), "power_of_attorney", None),
    (re.compile(r"\b(i'?m|i am) (a |the |their )?(professional )?fiduciary\b", re.I), "professional_fiduciary", None),
]
_NAME = re.compile(r"\b(?:call (?:him|her|them)|(?:his|her|their) name (?:is|was)|(?:named|called))\s+"
                   r"([A-Z][\w'-]+(?: [A-Z][\w'-]+)?)")

_CIRCUMSTANCE = [
    (re.compile(r"\b(police|investigat\w*|medical examiner|coroner|autopsy)\b", re.I), "under_investigation"),
    (re.compile(r"\b(accident\w*|crash\w*|car wreck|a fall|fell)\b", re.I), "accident_or_unexpected"),
    (re.compile(r"\b(unexpected\w*|out of nowhere)\b", re.I), "accident_or_unexpected"),
    (re.compile(r"\b(sudden\w*|without warning)\b", re.I), "sudden_natural"),
    (re.compile(r"\b(hospice|had been (ill|sick)|was (ill|sick)|long illness|after an illness|expected it|"
                r"we knew it was coming|palliative)\b", re.I), "expected_illness_or_hospice"),
]
_VETERAN_YES = re.compile(r"\b(veteran|served in the (army|navy|air force|marines?|marine corps|coast guard|military|"
                          r"national guard|space force)|(army|navy|marine|air force) vet)\b", re.I)
_VETERAN_NO = re.compile(r"\b(never served|wasn'?t in the military|not a veteran|never in the military)\b", re.I)
_WILL_NO = re.compile(r"\b(no will|didn'?t (have|leave|make) a will|did not (have|leave|make) a will|"
                      r"never (made|wrote) a will|without a will)\b", re.I)
_WILL_UNKNOWN = re.compile(r"\b(don'?t know|not sure|no idea) (if|whether) (he|she|they) (had|left|made) a will\b",
                           re.I)
_WILL_YES = re.compile(r"\b((had|has|left|made) a (will|trust)|(have|found) (the|a|his|her|their) will|"
                       r"(living )?trust|estate plan)\b", re.I)
_WILL_WHERE = re.compile(r"\b(safe|drawer|lawyer|attorney|file|filing cabinet|desk|i have (it|the will)|"
                         r"we have (it|the will)|found (the|his|her|their) will)\b", re.I)
_DONE = [
    (re.compile(r"\b(hospice|nurse|doctor|they) (pronounced|declared)|was pronounced\b", re.I), "death_pronounced"),
    (re.compile(r"\b(chose|picked|called|contacted|working with|went with|using) (a |the )?funeral home|"
                r"funeral home (is handling|has (him|her|them)|came)\b", re.I), "funeral_provider_chosen"),
    (re.compile(r"\bfuneral home has (his|her|their) social security( number)?\b", re.I), "funeral_home_has_ssn"),
    (re.compile(r"\b(ordered|requested) (the |some )?(death )?certificates\b", re.I), "certificates_ordered"),
    (re.compile(r"\b(told|called|notified) social security|social security (knows|was notified|has been notified)\b",
                re.I), "ssa_notified"),
    (re.compile(r"\b(told|called|notified) (the|their|his|her) bank|bank (knows|was notified)\b", re.I),
     "bank_notified"),
]

_INTENTS = {
    "pause": re.compile(r"\b(need (a|some) (break|minute|moment|time)|stop for now|take a break|pause|"
                        r"come back (to this )?later|can'?t (do this )?right now|not right now)\b", re.I),
    "done": re.compile(r"\b(that'?s all|that is all|that'?s everything|nothing else|that'?s it|"
                       r"that'?s all i know)\b", re.I),
    "continue": re.compile(r"\b(ready to (continue|keep going|go on)|let'?s (continue|keep going|go on)|"
                           r"keep going|i'?m ready)\b", re.I),
    "not_yet_died": re.compile(r"\b(hasn'?t (died|passed)|has not (died|passed)|not (dead|gone) yet|still alive|"
                               r"is dying|(is|she'?s|he'?s|they'?re) (in|on) hospice|near the end|actively dying|"
                               r"(days|weeks) left|expected to die)\b", re.I),
}
_ATTORNEY = {
    "contested_will": re.compile(r"\b((contest\w*|challeng\w*|fight\w*) (over |about )?(the )?will|"
                                 r"will (is being|was|is) contested|dispute (over|about) the will)\b", re.I),
    "family_disagreement": re.compile(r"\b(disagree\w*|can'?t agree|won'?t agree|fighting (over|about)|"
                                      r"arguing (over|about)|family (fight|feud|dispute))\b", re.I),
    "unsure_of_authority": re.compile(r"\b(am i allowed|do i have (the )?(authority|right|power)|"
                                      r"who (has|gets) (the )?(authority|say)|who'?s in charge|"
                                      r"not sure (if|whether) i (can|am allowed|have the right))\b", re.I),
    "multi_state_property": re.compile(r"\b((property|house|home|land|condo|cabin|real estate) in "
                                       r"(another|two|several|multiple|different|other) states?|"
                                       r"in (two|several|multiple|more than one|different) states)\b", re.I),
}


@dataclass
class Extraction:
    proposals: dict[FieldKey, tuple[object, str | None]] = field(default_factory=dict)
    away_from_home: bool = False
    intents: set[str] = field(default_factory=set)
    attorney_triggers: set[str] = field(default_factory=set)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def _state_in(sentence: str) -> tuple[str | None, str | None, str | None]:
    """(state code, county or city, matched words) for a place mentioned in a sentence."""
    m = _CITY_STATE.search(sentence)
    if m and m.group(2) in US_STATE_CODES:
        return m.group(2), m.group(1), m.group(0)
    m = _STATE_NAME.search(sentence)
    if m:
        county = _COUNTY.search(sentence)
        return STATE_NAMES[m.group(1).lower()], county.group(1) if county else None, m.group(0)
    return None, None, None


def _date_in(sentence: str, today: date) -> tuple[dict | None, str | None]:
    if m := _TODAY.search(sentence):
        return {"precision": "today", "date": today.isoformat()}, m.group(0)
    if m := _YESTERDAY.search(sentence):
        return {"precision": "exact", "date": (today - timedelta(days=1)).isoformat()}, m.group(0)
    if m := _DATE_UNKNOWN.search(sentence):
        return {"precision": "unknown", "date": None}, m.group(0)
    candidate, has_year = None, True
    if m := _ISO_DATE.search(sentence):
        candidate = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    elif m := _MONTH_DATE.search(sentence):
        has_year = m.group(3) is not None
        candidate = (int(m.group(3)) if has_year else today.year, _MONTHS[m.group(1).lower()], int(m.group(2)))
    elif m := _NUMERIC_DATE.search(sentence):
        year, has_year = m.group(3), m.group(3) is not None
        year = today.year if year is None else (2000 + int(year) if len(year) == 2 else int(year))
        candidate = (year, int(m.group(1)), int(m.group(2)))
    if candidate:
        try:
            d = date(*candidate)
            if d > today and not has_year:
                d = date(d.year - 1, d.month, d.day)  # a month and day later than today means last year
        except ValueError:
            d = None
        if d and d <= today:
            return {"precision": "exact", "date": d.isoformat()}, m.group(0)
    if m := _THIS_WEEK.search(sentence):
        return {"precision": "this_week", "date": None}, m.group(0)
    return None, None


def _valid(key: FieldKey, value: object) -> bool:
    try:
        TypeAdapter(FIELD_VALUE_TYPES[key]).validate_python(value)
    except ValidationError:
        return False
    return True


def extract(text: str, today: date) -> Extraction:
    out = Extraction()
    t = text.replace("’", "'")

    def propose(key: FieldKey, value: object, words: str | None):
        if key not in out.proposals and _valid(key, value):
            words = None if key == FieldKey.circumstance or not words else words[:120]
            out.proposals[key] = (value, words)

    for name, pattern in _INTENTS.items():
        if pattern.search(t):
            out.intents.add(name)
    for trigger, pattern in _ATTORNEY.items():
        if pattern.search(t):
            out.attorney_triggers.add(trigger)
    if "contested_will" in out.attorney_triggers:
        out.attorney_triggers.discard("family_disagreement")
    out.away_from_home = bool(_AWAY.search(t))

    for pattern, role, name in _ROLES:
        if m := pattern.search(t):
            propose(FieldKey.user_role, role, m.group(0))
            if name:
                propose(FieldKey.display_name, name, None)
            break
    if m := _NAME.search(t):
        propose(FieldKey.display_name, m.group(1), None)

    lived_state = None
    for sentence in _sentences(t):
        if lived := _LIVED_IN.search(sentence):
            # "She lived in Florida but died in Maine": the clause after "lived in" is the residence.
            rest = sentence[lived.end():]
            clause = re.split(r",| but | and ", rest, maxsplit=1)[0]
            lived_state = _state_in(clause)[0] or lived_state
            sentence = sentence[:lived.start()] + rest[len(clause):]
        if not _DEATH_WORDS.search(sentence):
            continue
        value, words = _date_in(sentence, today)
        if value:
            propose(FieldKey.date_of_death, value, words)
        if m := _ABROAD.search(sentence):
            propose(FieldKey.place_of_death, {"state": None, "county_or_city": None, "outside_us": True}, m.group(0))
        else:
            state, place, words = _state_in(sentence)
            if state:
                propose(FieldKey.place_of_death, {"state": state, "county_or_city": place, "outside_us": False}, words)

    death_state = (out.proposals.get(FieldKey.place_of_death, ({}, None))[0] or {}).get("state")
    if lived_state and death_state:
        same = lived_state == death_state
        propose(FieldKey.residence_state, {"choice": "same_as_place_of_death" if same else "different",
                                           "state": None if same else lived_state}, None)

    for pattern, value in _CIRCUMSTANCE:
        if pattern.search(t):
            propose(FieldKey.circumstance, value, None)
            break

    if _VETERAN_NO.search(t):
        propose(FieldKey.veteran_status, "no", None)
    elif m := _VETERAN_YES.search(t):
        propose(FieldKey.veteran_status, "yes", m.group(0))

    if m := _WILL_UNKNOWN.search(t):
        propose(FieldKey.estate_plan_status, "unknown", m.group(0))
    elif m := _WILL_NO.search(t):
        propose(FieldKey.estate_plan_status, "no", m.group(0))
    elif m := _WILL_YES.search(t):
        known = "yes_location_known" if _WILL_WHERE.search(t) else "yes_location_unknown"
        propose(FieldKey.estate_plan_status, known, m.group(0))

    done = [item for pattern, item in _DONE if pattern.search(t)]
    if done:
        propose(FieldKey.completed_items, done, None)
    return out
