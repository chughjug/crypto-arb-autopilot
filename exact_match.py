"""Market name matching: exact, same-event (core), and fuzzy tiers.

Handles cross-platform phrasing like nominees vs winner, qualifiers vs finals,
different word order, and $100k vs 100000 — without an LLM.
"""

from __future__ import annotations

import functools
import os
import re
from collections import defaultdict

STOP = {
    "the", "a", "an", "will", "be", "in", "by", "on", "of", "to", "for", "is",
    "are", "at", "and", "or", "vs", "us", "before", "after", "how", "many",
    "much", "who", "what", "which", "this", "that", "win", "wins", "next",
    "do", "does", "happen", "than", "more", "over", "under", "who", "market",
    "prediction", "predictions", "resolve", "resolved",
}

PHASE = {
    "nominee", "nominees", "nomination", "nominated", "nominations",
    "winner", "winners", "winning", "won",
    "qualifier", "qualifiers", "qualifying", "qualification",
    "shortlist", "shortlisted", "finalist", "finalists",
    "ceremony", "actual", "show", "live", "broadcast",
    "advance", "advanced", "advances",
    "name", "names", "named", "announce", "announced", "announcement",
    "pick", "picks", "picked", "select", "selected",
}

PHASE_DETECT = {
    "nomination": {"nominee", "nominees", "nomination", "nominated", "nominations", "shortlist"},
    "winner": {"winner", "winners", "winning", "won", "champion", "championship"},
    "ballot": {"ballot", "ballots"},
    "qualifier": {"qualifier", "qualifiers", "qualifying", "qualification"},
    "finals": {"final", "finals", "semifinal", "semifinals"},
    "ceremony": {"ceremony", "show", "broadcast", "actual"},
}

_WIN_SUBJECT = re.compile(
    r"^Will\s+(.+?)\s+win\s+(?:the\s+)?(?:\d{4}\s+)?",
    re.IGNORECASE,
)

ALIASES = {
    "oscars": "oscar", "academy": "oscar",
    "grammys": "grammy", "emmys": "emmy", "tonys": "tony",
    "gop": "republican", "dem": "democrat", "democrats": "democrat",
    "potus": "president", "vp": "vice", "presidential": "president",
    "btc": "bitcoin", "eth": "ethereum",
    "superbowl": "super",
    "f1": "formula",
    # concept synonyms — unify equivalent terms so the same question matches
    # across venues that word it differently (CPI vs inflation, YoY vs annual).
    "cpi": "inflation", "annual": "yearly", "annualized": "yearly",
    "yearonyear": "yearly", "monthonmonth": "monthly",
    "unemployment": "jobless", "gdp": "gdp",
}

# Multi-word phrases collapsed to a single concept token before tokenizing.
# Includes cross-platform vocabulary: Kalshi says "Pro Football/Basketball/
# Baseball/Hockey"; Polymarket says "NFL/NBA/MLB/NHL". Same leagues — normalize
# both to the league token so e.g. "Pro Football Champion" matches "NFL Champion".
_PHRASES = [
    (r"year[\s\-]*over[\s\-]*year", " yearly "),
    (r"month[\s\-]*over[\s\-]*month", " monthly "),
    (r"\byoy\b", " yearly "),
    (r"\bmom\b", " monthly "),
    (r"\bm/m\b", " monthly "),
    (r"\by/y\b", " yearly "),
    (r"consumer price index", " inflation "),
    (r"\bcore cpi\b", " core inflation "),
    # NFL/NHL-style season ranges "2026-27" -> just the starting year, so the
    # trailing "27" doesn't become a stray token that breaks the match.
    (r"\b(20\d\d)[-/]\d{2}\b", r" \1 "),
    # women's leagues must precede the generic "pro basketball" -> nba rule
    (r"women'?s pro basketball", " wnba "),
    (r"pro football", " nfl "),
    (r"pro basketball", " nba "),
    (r"pro baseball", " mlb "),
    (r"pro hockey", " nhl "),
    (r"super bowl", " nfl championship "),
    (r"world series", " mlb championship "),
    (r"stanley cup", " nhl championship "),
    (r"world soccer cup", " world cup "),     # Kalshi adds "Soccer"
    (r"\bepl\b", " premier league "),         # English Premier League
]

FUZZY_MIN = float(os.environ.get("FUZZY_MIN", "0.40"))
FUZZY_MIN_SHARED = int(os.environ.get("FUZZY_MIN_SHARED", "2"))
INDEX_MIN_OVERLAP = int(os.environ.get("INDEX_MIN_OVERLAP", "2"))


def _squash_numbers(text: str) -> str:
    def repl(m: re.Match) -> str:
        raw = m.group(0).replace(",", "").replace("$", "")
        try:
            if raw.lower().endswith("k"):
                return str(int(float(raw[:-1]) * 1000))
            if raw.lower().endswith("m"):
                return str(int(float(raw[:-1]) * 1_000_000))
            return str(int(float(raw))) if "." not in raw else raw
        except ValueError:
            return raw.lower()
    return re.sub(r"\$?\d[\d,\.]*[kKmM]?", repl, text)


@functools.lru_cache(maxsize=100_000)
def _tokenize(text: str) -> tuple[str, ...]:
    # Memoized: the same ~12k title strings are tokenized 1.6M times across the
    # match (every _classify + every guard), so caching is the dominant speedup.
    # Returns a tuple so the cached value can't be mutated by callers.
    if not text:
        return ()
    s = text.lower().strip()
    for pat, rep in _PHRASES:        # collapse multi-word concepts first
        s = re.sub(pat, rep, s)
    s = _squash_numbers(s)
    s = re.sub(r"[^\w\s]", " ", s)
    out: list[str] = []
    for w in s.split():
        if len(w) <= 1 or w in STOP:
            continue
        w = w.rstrip("s")
        w = ALIASES.get(w, w)
        if w and w not in STOP:
            out.append(w)
    return tuple(out)


def _years(tokens: list[str]) -> list[str]:
    return sorted({t for t in tokens if re.fullmatch(r"20\d{2}", t)})


def canonical(text: str) -> str:
    return " ".join(sorted(_tokenize(text)))


@functools.lru_cache(maxsize=100_000)
def core_tokens(text: str) -> frozenset[str]:
    toks = _tokenize(text)
    yrs = set(_years(toks))
    return frozenset(t for t in toks if t not in PHASE and t not in yrs)


def core_key(text: str) -> str:
    toks = _tokenize(text)
    yrs = _years(toks)
    core = sorted(t for t in toks if t not in PHASE and t not in yrs)
    return "|".join(yrs) + "|" + " ".join(core)


def contract_subject(label: str) -> str:
    """Extract the subject from Kalshi-style questions, e.g. 'Will X win…' -> X."""
    lab = (label or "").strip()
    if not lab:
        return lab
    m = _WIN_SUBJECT.match(lab)
    if m:
        return m.group(1).strip()
    low = lab.lower()
    if low.startswith("will ") and " win " in low:
        body = lab[5:]
        idx = body.lower().find(" win ")
        if idx > 0:
            return body[:idx].strip()
    return lab


def _phases(text: str) -> list[str]:
    s = (text or "").lower()
    out: list[str] = []
    toks = set(_tokenize(text))
    for name, words in PHASE_DETECT.items():
        if toks & words:
            out.append(name)
    if "ballot" in s and "ballot" not in out:
        out.append("ballot")
    if re.search(r"\bwho will win\b", s) or re.search(r"\bwill\s+.+\s+win\b", s):
        if "winner" not in out:
            out.append("winner")
    return out


# --- sport / scope guard ------------------------------------------------------
# Title tokens like "champion", "winner", "2027" overlap heavily across unrelated
# sports markets (NFC Champion vs NBA Champion), and the sports fuzzy matcher
# pairs them via shared host cities (Detroit Lions / Detroit Pistons). Detect the
# sport and the scope qualifier so we can reject those mismatches outright.
SPORT_MARKERS = {
    "football": ("pro football", "nfl", "nfc", "afc", "super bowl", "superbowl"),
    "basketball": ("pro basketball", "nba", "wnba"),
    "baseball": ("pro baseball", "mlb", "world series"),
    "hockey": ("pro hockey", "nhl", "stanley cup"),
    "soccer": ("premier league", "la liga", "uefa", "champions league",
               "world cup", "bundesliga", "serie a", "ligue 1", "epl"),
    "esports": ("counter-strike", "counter strike", "csgo", "cs2", "dota",
                "valorant", "league of legends", "lol:", "rocket league",
                "overwatch", "rainbow six", "starcraft", "call of duty", "esports"),
}
_CONFERENCES = {"nfc", "afc"}
_CONF_WORDS = {"eastern", "western"}
_DIVISIONS = {"east", "west", "north", "south", "central"}
_SCOPE_QUALIFIERS = _CONFERENCES | _CONF_WORDS | _DIVISIONS | {"conference", "division"}
_PLAYER_STAT = re.compile(
    r"\b\d+\+?\s*(?:passing|rushing|receiving)\s+yard|"
    r"\b(?:passing|rushing|receiving)\s+yard|\byardage\b|"
    r"\btouchdown\s+leader|\binterception|\bsack\s+leader|"
    r"\bhome\s+run\s+leader|\bstrikeout\s+leader",
    re.IGNORECASE,
)
_WC_GROUP = re.compile(r"\bgroup\s+[a-l]\b", re.IGNORECASE)
_WC_HOST = re.compile(r"\bhost\b", re.IGNORECASE)


@functools.lru_cache(maxsize=100_000)
def _sports(text: str) -> frozenset[str]:
    s = f" {(text or '').lower()} "
    found = {sport for sport, markers in SPORT_MARKERS.items()
             if any(m in s for m in markers)}
    return frozenset(found)


_ACQUIRE_VERBS = {
    "buy", "buys", "buying", "bought", "purchase", "purchases", "purchasing",
    "purchased", "acquire", "acquires", "acquiring", "acquired",
}


def _acquisition_object_conflict(a: str, b: str) -> bool:
    """True for "Will <X> buy <Y>?" pairs with the same subject but different object.

    Celebrity/company acquisition markets share a subject and verb ("Will Elon
    Musk buy ...") but resolve on totally different objects (a sports team vs
    OnlyFans). Token overlap pairs them; compare the objects (tokens after the
    acquisition verb) and reject when they're disjoint.
    """
    ta, tb = _tokenize(a), _tokenize(b)
    if not (set(ta) & _ACQUIRE_VERBS and set(tb) & _ACQUIRE_VERBS):
        return False

    def obj(toks: list[str]) -> set[str]:
        for i, t in enumerate(toks):
            if t in _ACQUIRE_VERBS:
                return {x for x in toks[i + 1:]
                        if len(x) > 2 and not x.isdigit() and x not in PHASE
                        and x not in {"the", "a", "an", "before", "after", "this", "next"}}
        return set()

    oa, ob = obj(ta), obj(tb)
    return bool(oa and ob and not (oa & ob))


# Economic indicators are identified by region + period as much as by metric.
# After synonym normalization, "US CPI YoY June" and "Brazil Annual Inflation"
# both reduce to {inflation, yearly} and would falsely match — so an indicator
# market's country and month are load-bearing.
# A macro data-release market is identified by region + metric + sub-index +
# period-type + month. After "cpi"->"inflation" normalization these all reduce
# toward {inflation, yearly}, so each dimension below is load-bearing.
_METRICS = {  # distinct data series — a market is about exactly one
    "inflation", "gdp", "jobless", "unemployment", "payroll", "export", "import",
    "trade", "retail", "pmi", "pce", "wage", "earnings", "gasoline", "shelter",
}
_SUBINDEX = {  # CPI sub-indices — different markets from headline CPI
    "core", "gasoline", "shelter", "airline", "airfare", "food", "energy", "rent",
    "medical", "apparel", "used", "grocery", "gas",
}
# Foreign-country markers checked as substrings (handles "U.K.", "U.S." which
# tokenize to single chars). US/America are the implicit default → not listed.
_FOREIGN = (
    "brazil", "india", "china", "mexico", "canada", "eurozone", "euro area",
    "europe", "korea", "africa", "argentina", "u.k", "uk ", "britain", "british",
    "japan", "german", "france", "french", "australia", "turkey", "russia",
    "indonesia", "nigeria", "colombia", "chile", "peru", "spain", "italy", "poland",
)
_MONTHS = {
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
}


@functools.lru_cache(maxsize=100_000)
def _foreign_country(text: str) -> frozenset[str]:
    s = f" {(text or '').lower()} "
    return frozenset(c.strip() for c in _FOREIGN if c in s)


def _period_type(tokens: set[str]) -> str:
    if "monthly" in tokens:
        return "monthly"
    if "yearly" in tokens:
        return "yearly"
    if tokens & _MONTHS:            # a specific month, no annual marker -> monthly figure
        return "monthly"
    return "?"


def _indicator_conflict(a: str, b: str) -> bool:
    """True if two macro data-release markets differ on any identity dimension."""
    ta, tb = set(_tokenize(a)), set(_tokenize(b))
    ma_metric, mb_metric = ta & _METRICS, tb & _METRICS
    if not (ma_metric and mb_metric):
        return False                     # not both macro-indicator markets
    if ma_metric != mb_metric:
        return True                      # different series (exports vs inflation, core vs headline)
    if (ta & _SUBINDEX) != (tb & _SUBINDEX):
        return True                      # gasoline/core/shelter CPI vs headline CPI
    fa, fb = _foreign_country(a), _foreign_country(b)
    if fa != fb:
        return True                      # different country (incl. foreign vs US-default)
    pa, pb = _period_type(ta), _period_type(tb)
    if pa != "?" and pb != "?" and pa != pb:
        return True                      # monthly vs yearly
    ams, bms = ta & _MONTHS, tb & _MONTHS
    if ams and bms and not (ams & bms):
        return True                      # June vs July
    return False


_PLACE_WORDS = {
    "south", "north", "east", "west", "united", "states", "state", "kingdom",
    "korea", "africa", "america", "american", "us", "republic", "saudi", "new",
} | {c for w in _FOREIGN for c in w.replace(".", " ").split()}


def _place_collision_conflict(a: str, b: str) -> bool:
    """Two events whose only shared content is a place/time (e.g. 'South Korea
    Goalscorers' vs 'South Korea Inflation') are different questions."""
    ca, cb = core_tokens(a), core_tokens(b)
    shared = ca & cb
    if not shared:
        return False
    substantive = {t for t in shared
                   if t not in _PLACE_WORDS and t not in _MONTHS and not t.isdigit()}
    if substantive:
        return False                     # they share real topic content — fine
    da = {t for t in (ca - cb) if t not in _PLACE_WORDS and t not in _MONTHS and not t.isdigit()}
    db = {t for t in (cb - ca) if t not in _PLACE_WORDS and t not in _MONTHS and not t.isdigit()}
    return bool(da and db)               # each side has its own distinct topic


_MONTH_NUM = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}


@functools.lru_cache(maxsize=100_000)
def _resolution_period(text: str):
    """Best-effort (year, month) the market resolves on, from title+subtitle.

    Handles "On Dec 31, 2026", "Before 2027" (=> end of 2026), "by June 30",
    "end of July", "this year". Either field may be None when not stated.
    """
    s = (text or "").lower()
    month = None
    for name in sorted(_MONTH_NUM, key=len, reverse=True):
        if re.search(rf"\b{name}\b", s):
            month = _MONTH_NUM[name]
            break
    year = None
    mb = re.search(r"\bbefore\s+(20\d\d)\b", s)
    if mb:
        year = int(mb.group(1)) - 1      # "before 2027" resolves in 2026
        if month is None:
            month = 12
    else:
        my = re.search(r"\b(20\d\d)\b", s)
        if my:
            year = int(my.group(1))
    if month is None and re.search(r"end of (the )?year|this year|year[- ]end", s):
        month = 12
    if month is None and year is None:
        return None
    return (year, month)


def _date_conflict(a: str, b: str) -> bool:
    """True if two markets resolve on clearly different dates (deadline matters)."""
    pa, pb = _resolution_period(a), _resolution_period(b)
    if not pa or not pb:
        return False
    (ya, ma), (yb, mb) = pa, pb
    if ya and yb and ya != yb:
        return True                      # different resolution year
    if ma and mb and ma != mb:
        return True                      # different resolution month (June vs Dec)
    return False


# A price market resolves either on a SNAPSHOT (value at a point in time:
# "at the end of 2026") or PATH-dependent (whether it ever hits/reaches a level,
# or its all-time-high). Same asset + same tokens, but different questions.
_PATH_WORDS = (
    "hit ", "hits ", "reach", "all time high", "all-time high", "all time-high",
    " ath", "ever ", "touch", "surpass", "crosses ", "cross ", "highest",
    "when will",
)
_SNAPSHOT_WORDS = ("end of", "at the end", "year-end", "year end", "closing price")
_VALUE_HINTS = (
    "price", "$", "bitcoin", "ethereum", "solana", " btc", " eth", "xrp",
    "dogecoin", "all time high", "market cap",
)


@functools.lru_cache(maxsize=100_000)
def _value_resolution_type(s: str) -> str:
    if not any(h in s for h in _VALUE_HINTS):
        return "n/a"
    path = any(w in s for w in _PATH_WORDS)
    snap = any(w in s for w in _SNAPSHOT_WORDS)
    if path and not snap:
        return "path"
    if snap and not path:
        return "snapshot"
    return "?"


def _resolution_type_conflict(a: str, b: str) -> bool:
    """Snapshot price vs path-dependent (hit/all-time-high) are different markets."""
    ta, tb = _value_resolution_type(a.lower()), _value_resolution_type(b.lower())
    return ta in ("path", "snapshot") and tb in ("path", "snapshot") and ta != tb


_ELECTION_WORDS = re.compile(
    r"\b(governor|gubernatorial|senate|senator|president|presidential|house|"
    r"election|mayor|mayoral|nominee|nomination|primary|primaries|"
    r"parliament|parliamentary|turnout|ballot|midterm)\b"
)
_PRIMARY_WORDS = re.compile(r"\bprimar(?:y|ies)\b|\bnomin(?:ee|ation|ated|ate)\b")
_GENERAL_WORDS = re.compile(r"\bgeneral\b|\bwinner\b|\bwin\b")
_TURNOUT = re.compile(r"\bturnout\b", re.IGNORECASE)

# US states + foreign countries for election geo guard. District codes like
# "AL-01" / "Alabama 01" canonicalize to DIST:AL-01 so Kalshi and Polymarket
# formats align.
_US_STATE_NAMES: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH",
    "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}
_US_STATE_ABBRS = frozenset(_US_STATE_NAMES.values())
_ELECTION_FOREIGN = (
    "new zealand", "euro area", "eurozone", "south korea", "north korea",
    "united kingdom", "saudi arabia", "south africa",
    "brazil", "india", "china", "mexico", "canada", "europe", "korea", "africa",
    "argentina", "u.k", "uk ", "britain", "british", "japan", "german", "france",
    "french", "australia", "turkey", "russia", "indonesia", "nigeria", "colombia",
    "chile", "peru", "spain", "italy", "poland", "ethiopia", "slovakia", "slovak",
    "quebec", "iran", "venezuela", "ukraine", "israel", "philippines", "pakistan",
    "bangladesh", "egypt", "kenya", "ghana", "hungary", "romania", "czech",
    "netherlands", "dutch", "sweden", "norway", "denmark", "finland", "portugal",
    "greece", "austria", "belgium", "switzerland", "taiwan", "thailand", "vietnam",
    "malaysia", "turkish", "sweden", "swedish", "norwegian", "finnish", "danish",
    "mexican", "canadian", "australian",
)
# "house" only when clearly a legislative chamber, not "White House".
_HOUSE_OFFICE = re.compile(
    r"\b(?:u\.?s\.?\s+)?house\b(?!\s+of\s+representatives\b)|"
    r"\bhouse\s+of\s+representatives\b|"
    r"\bhouse\s+election\b|"
    r"\bhouse\s+seat\b|"
    r"\b[a-z]{2}-\d{2}\b",
    re.IGNORECASE,
)
_MARGIN = re.compile(r"\bmargin\b", re.IGNORECASE)
_PLACEMENT = re.compile(r"\b(\d+)(?:st|nd|rd|th)\s+place\b", re.IGNORECASE)


@functools.lru_cache(maxsize=100_000)
def _election_offices(text: str) -> frozenset[str]:
    s = (text or "").lower()
    found: set[str] = set()
    if re.search(r"\bgovernor", s):
        found.add("governor")
    if re.search(r"\bsenat", s):
        found.add("senate")
    if re.search(r"\bpresiden", s):
        found.add("president")
    if re.search(r"\bmayor", s):
        found.add("mayor")
    if re.search(r"\bparliament", s):
        found.add("parliament")
    if _HOUSE_OFFICE.search(text or ""):
        found.add("house")
    return frozenset(found)


@functools.lru_cache(maxsize=100_000)
def _election_phase(s: str) -> str | None:
    """Classify an election market as 'primary' or 'general'.

    A party primary / nominee race (one party's voters pick a candidate) is a
    different market than the general election (all candidates, all voters),
    even though both share the office, state, and year. Returns None when the
    text isn't clearly an election market or the phase can't be told.
    """
    s = s.lower()
    if not _ELECTION_WORDS.search(s):
        return None
    if _PRIMARY_WORDS.search(s):
        return "primary"
    if _GENERAL_WORDS.search(s):
        return "general"
    return None


def _election_phase_conflict(a: str, b: str) -> bool:
    """True if one side is a party primary/nominee race and the other is the
    general election — same office and year, but a different question."""
    pa, pb = _election_phase(a), _election_phase(b)
    return pa is not None and pb is not None and pa != pb


def _is_election_market(text: str) -> bool:
    return bool(_ELECTION_WORDS.search((text or "").lower()))


@functools.lru_cache(maxsize=100_000)
def _election_geo_entities(text: str) -> frozenset[str]:
    """Canonical geographic entities for election markets (US state, district, country)."""
    raw = text or ""
    s = f" {raw.lower()} "
    found: set[str] = set()
    matched_spans: list[tuple[int, int]] = []
    for name, abbr in sorted(_US_STATE_NAMES.items(), key=lambda x: -len(x[0])):
        needle = f" {name} "
        idx = s.find(needle)
        if idx >= 0:
            found.add(f"US:{abbr}")
            matched_spans.append((idx, idx + len(needle)))
    for m in re.finditer(r"\b([A-Za-z]{2})-(\d{1,2})\b", raw):
        st = m.group(1).upper()
        if st in _US_STATE_ABBRS:
            found.add(f"DIST:{st}-{int(m.group(2)):02d}")
    for m in re.finditer(r"\b([a-z]{2,})\s+(\d{1,2})\b", s):
        st = _US_STATE_NAMES.get(m.group(1))
        if st:
            found.add(f"DIST:{st}-{int(m.group(2)):02d}")
    for country in sorted(_ELECTION_FOREIGN, key=len, reverse=True):
        pos = s.find(country)
        if pos < 0:
            continue
        if any(start <= pos < end for start, end in matched_spans):
            continue
        found.add(f"FN:{country.strip()}")
    return frozenset(found)


def _turnout_conflict(a: str, b: str) -> bool:
    """Voter-turnout % markets are a different question than who-wins markets."""
    return bool(_TURNOUT.search(a)) != bool(_TURNOUT.search(b))


def _election_geo_conflict(a: str, b: str) -> bool:
    """Two election markets about clearly different places are not the same event."""
    if not (_is_election_market(a) and _is_election_market(b)):
        return False
    ga, gb = _election_geo_entities(a), _election_geo_entities(b)
    if not ga or not gb:
        return False
    return not (ga & gb)


def _election_office_conflict(a: str, b: str) -> bool:
    """Governor vs Senate vs House races are different markets even in the same state."""
    if not (_is_election_market(a) and _is_election_market(b)):
        return False
    oa, ob = _election_offices(a), _election_offices(b)
    if not oa or not ob:
        return False
    return not (oa & ob)


def _margin_conflict(a: str, b: str) -> bool:
    """Margin-of-victory markets differ from who-wins / placement markets."""
    return bool(_MARGIN.search(a)) != bool(_MARGIN.search(b))


def _placement_conflict(a: str, b: str) -> bool:
    """2nd-place and 3rd-place finisher markets are not the same contract."""
    pa = {int(n) for n in _PLACEMENT.findall(a or "")}
    pb = {int(n) for n in _PLACEMENT.findall(b or "")}
    return bool(pa) and bool(pb) and pa != pb


def _player_stat_market(text: str) -> bool:
    return bool(_PLAYER_STAT.search(text or ""))


def _player_stat_conflict(a: str, b: str) -> bool:
    """Player stat thresholds (3000+ passing yards) != league/CBA/champion markets."""
    sa, sb = _sports(a), _sports(b)
    if not (sa and sb and (sa & sb)):
        return False
    return _player_stat_market(a) != _player_stat_market(b)


def _world_cup_scope_conflict(a: str, b: str) -> bool:
    """Group-stage, host-city, and overall World Cup markets are different scopes."""
    def _is_world_cup(s: str) -> bool:
        sl = (s or "").lower()
        return "world cup" in sl or "world soccer cup" in sl

    if not (_is_world_cup(a) or _is_world_cup(b)):
        return False
    ga = bool(_WC_GROUP.search(a or ""))
    gb = bool(_WC_GROUP.search(b or ""))
    ha = bool(_WC_HOST.search(a or ""))
    hb = bool(_WC_HOST.search(b or ""))
    return ga != gb or ha != hb


def _gender_conflict(a: str, b: str) -> bool:
    """Women's vs men's/default league are different competitions (WNBA != NBA)."""
    if not (_sports(a) or _sports(b)):
        return False
    pat = r"\b(women|women's|womens|wnba|ladies|female)\b"
    return bool(re.search(pat, a.lower())) != bool(re.search(pat, b.lower()))


def _no_shared_content(a: str, b: str) -> bool:
    """True if the two events share NO content token at all. The fuzzy/immike
    matchers can over-score on shared structure + year ("Will AI be charged ...
    before 2027?" vs "Will xAI release a video game before 2027?" -> 0.78), but
    with zero common subject they cannot be the same market."""
    ca, cb = core_tokens(a), core_tokens(b)
    return bool(ca) and bool(cb) and not (ca & cb)


def _sport_scope_conflict(a: str, b: str) -> bool:
    """True if two events clearly cover different sports/scopes/objects/dates."""
    if _no_shared_content(a, b):
        return True                      # nothing in common but structure/year
    if _date_conflict(a, b):
        return True                      # different resolution / expiration date
    if _resolution_type_conflict(a, b):
        return True                      # snapshot price vs hit/all-time-high
    if _gender_conflict(a, b):
        return True                      # women's league vs men's/default
    if _election_phase_conflict(a, b):
        return True                      # party primary/nominee vs general election
    if _turnout_conflict(a, b):
        return True                      # turnout % vs who-wins
    if _election_geo_conflict(a, b):
        return True                      # different state/district/country
    if _election_office_conflict(a, b):
        return True                      # governor vs senate vs house
    if _margin_conflict(a, b):
        return True                      # margin of victory vs who-wins
    if _placement_conflict(a, b):
        return True                      # 2nd place vs 3rd place
    if _acquisition_object_conflict(a, b):
        return True                      # same buyer, different acquisition target
    if _indicator_conflict(a, b):
        return True                      # different region/month economic indicator
    if _place_collision_conflict(a, b):
        return True                      # shared only a place/time, different topic
    sa, sb = _sports(a), _sports(b)
    if sa and sb and not (sa & sb):
        return True                      # different sports (football vs basketball)
    if not (sa or sb):
        return False                     # not a sports market — skip directional guards
    if _player_stat_conflict(a, b):
        return True                      # player stat threshold vs unrelated football market
    if _world_cup_scope_conflict(a, b):
        return True                      # group-stage vs host vs overall winner
    ta, tb = set(_tokenize(a)), set(_tokenize(b))
    for group in (_CONFERENCES, _CONF_WORDS):
        ga, gb = ta & group, tb & group
        if ga and gb and not (ga & gb):
            return True                  # NFC vs AFC, Eastern vs Western
    da, db = ta & _DIVISIONS, tb & _DIVISIONS
    if da and db and not (da & db):
        return True                      # NFC East vs NFC South
    qa, qb = ta & _SCOPE_QUALIFIERS, tb & _SCOPE_QUALIFIERS
    if bool(qa) != bool(qb):
        return True                      # conference/division champ vs overall champ
    return False


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# Inverse-document-frequency weights over the live corpus. A token shared by
# many events (a popular subject like "elon", or a boilerplate word like
# "inflation" when 25 inflation markets exist) carries little signal; a rare
# token (onlyfans, brazil, ryanair) carries a lot. Weighting the similarity by
# IDF means two events that overlap only on ubiquitous tokens score LOW without
# any blocklist, while genuinely-distinctive overlaps score high.
_IDF: dict[str, float] = {}


def set_corpus_idf(texts: list[str]) -> None:
    import math
    df: dict[str, int] = defaultdict(int)
    n = 0
    for t in texts:
        n += 1
        for tok in set(_tokenize(t)):
            df[tok] += 1
    _IDF.clear()
    if not n:
        return
    for tok, d in df.items():
        _IDF[tok] = math.log((n + 1) / (d + 1)) + 1.0
    # default weight for tokens not seen in the corpus = treat as fairly rare
    _IDF["__default__"] = math.log((n + 1) / 1) + 1.0


def _w(tok: str) -> float:
    return _IDF.get(tok, _IDF.get("__default__", 1.0))


def _weighted_jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """IDF-weighted Jaccard; falls back to plain Jaccard if no corpus is set."""
    if not a or not b:
        return 0.0
    if not _IDF:
        return len(a & b) / len(a | b)
    inter = sum(_w(t) for t in (a & b))
    union = sum(_w(t) for t in (a | b))
    return inter / union if union else 0.0


def _classify(a: str, b: str) -> dict:
    ca, cb = canonical(a), canonical(b)
    core_a, core_b = core_tokens(a), core_tokens(b)
    ck_a, ck_b = core_key(a), core_key(b)
    sim = _weighted_jaccard(core_a, core_b)
    shared = sorted(core_a & core_b)
    phases_a, phases_b = _phases(a), _phases(b)

    exact = bool(ca) and ca == cb
    same_core = bool(core_a) and core_a == core_b
    yrs_a, yrs_b = set(_years(_tokenize(a))), set(_years(_tokenize(b)))
    year_ok = not yrs_a or not yrs_b or bool(yrs_a & yrs_b)
    fuzzy_ok = year_ok and len(shared) >= FUZZY_MIN_SHARED and sim >= FUZZY_MIN

    if exact:
        tier = "exact"
    elif same_core and year_ok:
        tier = "same_event"
    elif fuzzy_ok:
        tier = "similar"
    else:
        tier = "different"

    # ImMike fuzzy matcher (sports teams, entity overlap, SequenceMatcher)
    # Skip per-pair ImMike on Heroku — find_event_pairs runs it in bulk instead.
    immike_sim = 0.0
    try:
        import immike_match
        if immike_match.IMMIKE_ENABLED and not os.environ.get("DYNO"):
            immike_sim = immike_match.calculate_similarity(a, b)
            if tier == "different" and immike_sim >= immike_match.IMMIKE_MIN_SIM:
                tier = "same_event" if immike_sim >= 0.82 else "similar"
                sim = max(sim, immike_sim)
            elif immike_sim > sim:
                sim = immike_sim
    except ImportError:
        pass

    # Reject cross-sport / cross-scope pairs even if tokens or the sports fuzzy
    # matcher suggested a match (NFC Champion vs NBA Champion, etc.).
    if tier != "different" and _sport_scope_conflict(a, b):
        tier = "different"

    phase_warning = bool(phases_a or phases_b) and phases_a != phases_b

    return {
        "exact_match": exact,
        "same_event": tier in ("exact", "same_event", "similar"),
        "match_tier": tier,
        "similarity": round(sim, 3),
        "shared_core": shared,
        "phase_a": phases_a,
        "phase_b": phases_b,
        "phase_warning": phase_warning,
        "a": a.strip(),
        "b": b.strip(),
        "canonical_a": ca,
        "canonical_b": cb,
        "core_a": ck_a,
        "core_b": ck_b,
        "immike_similarity": round(immike_sim, 3) if immike_sim else None,
    }


def _merge_ai(out: dict, ai: dict, engine: str) -> dict:
    """Apply AI compare result onto rule-based output."""
    out[engine] = ai
    out["matcher"] = f"hybrid+{engine}"
    rel = ai.get("relationship", "unrelated")
    p_yes = ai.get("similarity", 0.0)
    rule_tier = out["match_tier"]

    # Strong GPT-2 yes upgrades weak rule matches; strong no only downgrades weak tiers.
    if p_yes >= 0.72 and rule_tier in ("different", "similar"):
        out["same_event"] = True
        out["match_tier"] = max_tier(rule_tier, "same_event")
    elif p_yes < 0.38 and rule_tier in ("similar",):
        out["same_event"] = False
        out["match_tier"] = "different"
    elif ai.get("same_event") and rule_tier == "different":
        out["same_event"] = True
        out["match_tier"] = "similar"

    if out.get("phase_warning"):
        out["same_resolution"] = False
    elif rel == "identical" and p_yes >= 0.78:
        out["same_resolution"] = True
        if not out.get("phase_warning"):
            out["match_tier"] = max_tier(out["match_tier"], "same_event")
    elif ai.get("same_resolution") and not out.get("phase_warning"):
        out["same_resolution"] = True
    else:
        out["same_resolution"] = out.get("same_resolution", False) and not out.get("phase_warning")

    out["exact_match"] = out["match_tier"] == "exact"
    if ai.get("similarity") is not None:
        out["similarity"] = ai["similarity"]
    if ai.get("explanation"):
        out["explanation"] = ai["explanation"]
    return out


def max_tier(current: str, proposed: str) -> str:
    order = {"different": 0, "similar": 1, "same_event": 2, "exact": 3}
    return proposed if order.get(proposed, 0) > order.get(current, 0) else current


def compare(a: str, b: str, use_llm: bool = False, use_gpt2: bool = False) -> dict:
    """Compare two market names; rules plus optional GPT-2 or semantic matchers."""
    out = _classify(a, b)
    out["matcher"] = "rules"
    out["same_resolution"] = out["exact_match"]

    if use_gpt2:
        try:
            import gpt2_match
            ai = gpt2_match.compare(a, b)
        except ImportError:
            ai = None
        if ai:
            out = _merge_ai(out, ai, "gpt2")
        else:
            st = {}
            try:
                import gpt2_match as g2
                st = g2.status()
            except ImportError:
                st = {"error": "install requirements-gpt2.txt"}
            out["gpt2"] = None
            out["gpt2_error"] = st.get("error") or (
                "GPT-2 not ready — run: pip install -r requirements-gpt2.txt"
            )

    if use_llm:
        llm = None
        try:  # prefer the configured semantic matcher when available
            import llm_match
            if llm_match.ENABLED:
                llm = llm_match.reason_compare(a, b)
        except ImportError:
            pass
        if llm is None:
            try:
                import ollama_match
                llm = ollama_match.reason_compare(a, b)
            except ImportError:
                llm = None
        if llm:
            out = _merge_ai(out, llm, "llm")
        elif not use_gpt2 or not out.get("gpt2"):
            out["llm"] = None
            out["llm_error"] = (
                "Semantic matcher unavailable — use GPT-2 (?gpt2=1)"
            )

    return out


def event_key(event: dict) -> str:
    parts = [event.get("title") or "", event.get("subtitle") or ""]
    return canonical(" ".join(p for p in parts if p))


def event_core_key(event: dict) -> str:
    parts = [event.get("title") or "", event.get("subtitle") or ""]
    return core_key(" ".join(p for p in parts if p))


def contract_key(label: str) -> str:
    return canonical(label or "")


def find_event_pairs(
    kalshi: list[dict], poly: list[dict]
) -> list[tuple[int, int, str]]:
    """Pair events using inverted index + exact/core/fuzzy tiers."""
    p_texts: list[str] = []
    p_core: list[frozenset[str]] = []
    k_texts: list[str] = [
        " ".join(filter(None, [ke.get("title"), ke.get("subtitle")])) for ke in kalshi
    ]
    index: dict[str, list[int]] = defaultdict(list)
    for j, pe in enumerate(poly):
        pa = " ".join(filter(None, [pe.get("title"), pe.get("subtitle")]))
        p_texts.append(pa)
        core = core_tokens(pa)
        p_core.append(core)
        for t in core:
            if len(t) > 2:
                index[t].append(j)

    cands: list[tuple[float, int, int, str]] = []
    for i, ke in enumerate(kalshi):
        ka = " ".join(filter(None, [ke.get("title"), ke.get("subtitle")]))
        kcore = core_tokens(ka)
        if not kcore:
            continue
        overlap: dict[int, int] = defaultdict(int)
        for t in kcore:
            for j in index.get(t, []):
                overlap[j] += 1
        for j, cnt in overlap.items():
            if cnt < INDEX_MIN_OVERLAP:
                continue
            r = _classify(ka, p_texts[j])
            tier = r["match_tier"]
            # Respect the classifier. The old code promoted "different" pairs to
            # "similar" whenever similarity cleared FUZZY_MIN-0.08, which let
            # events sharing only a named entity (every "Elon Musk ..." market vs
            # every other "Elon Musk ..." market) match. If it's below the fuzzy
            # bar, it is not a match.
            if tier == "different":
                continue
            rank = {"exact": 3, "same_event": 2, "similar": 1}[tier]
            cands.append((rank + r["similarity"], i, j, tier))

    # ImMike category-bucketed pairs (sports/politics/crypto)
    try:
        import immike_match
        if immike_match.IMMIKE_ENABLED and not os.environ.get("DYNO"):
            seen = {(i, j) for _, i, j, _ in cands}
            for i, j, tier in immike_match.find_event_pairs(kalshi, poly):
                if (i, j) in seen:
                    continue
                ka_i = " ".join(filter(None, [kalshi[i].get("title"), kalshi[i].get("subtitle")]))
                rank = {"exact": 3, "same_event": 2, "similar": 1}[tier]
                score = immike_match.calculate_similarity(p_texts[j], ka_i)
                cands.append((rank + score - 0.01, i, j, tier))
    except ImportError:
        pass

    cands.sort(reverse=True)
    used_k: set[int] = set()
    used_p: set[int] = set()
    out: list[tuple[int, int, str]] = []
    for _, i, j, tier in cands:
        if i in used_k or j in used_p:
            continue
        # final guard — also covers pairs added by the bulk sports matcher,
        # which bypasses _classify.
        if _sport_scope_conflict(k_texts[i], p_texts[j]):
            continue
        used_k.add(i)
        used_p.add(j)
        out.append((i, j, tier))
    return out


def find_exact_event_pairs(
    kalshi: list[dict], poly: list[dict]
) -> list[tuple[int, int, str]]:
    return find_event_pairs(kalshi, poly)


def find_exact_contract_pairs(
    kmarkets: list[dict], pmarkets: list[dict]
) -> list[tuple[int, int]]:
    """Pair outcomes: exact label, or core label match."""
    by_canon: dict[str, list[int]] = defaultdict(list)
    by_core: dict[str, list[int]] = defaultdict(list)
    for j, pm in enumerate(pmarkets):
        lab = pm.get("label") or ""
        ck = contract_key(lab)
        if ck:
            by_canon[ck].append(j)
        core = " ".join(sorted(core_tokens(lab)))
        if core:
            by_core[core].append(j)

    pairs: list[tuple[int, int]] = []
    used_p: set[int] = set()
    for i, km in enumerate(kmarkets):
        lab = contract_subject(km.get("label") or "")
        pm_j = None
        ck = contract_key(lab)
        if ck and ck in by_canon:
            for j in by_canon[ck]:
                if j not in used_p:
                    pm_j = j
                    break
        if pm_j is None:
            core = " ".join(sorted(core_tokens(lab)))
            if core and core in by_core:
                for j in by_core[core]:
                    if j not in used_p:
                        pm_j = j
                        break
        if pm_j is not None:
            used_p.add(pm_j)
            pairs.append((i, pm_j))
    return pairs
