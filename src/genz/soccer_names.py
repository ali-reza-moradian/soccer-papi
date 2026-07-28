"""SOCCER CLUB-NAME normalizer + scored matcher — the European-pairing fix.

WHY THIS EXISTS (evidence: tests/fixtures/raw_soccer_*.json, captured 2026-07-28)

The two venues spell the same European club very differently. Kalshi uses a short broadcast name;
Polymarket uses the club's registered name, with legal form, sponsor and city:

    Kalshi                  Polymarket                      what differs
    ----------------------  ------------------------------  -----------------------------------
    Dinamo Zagreb           GNK Dinamo Zagreb               legal-form prefix
    Sturm Graz              SK Puntigamer Sturm Graz         legal form + SPONSOR token
    Real Sociedad           Real Sociedad San Sebastian      city suffix
    Lech Poznan             KKS Lech Poznań                  legal form + diacritic
    Egnatia Rrogozhine      KF Egnatia Rrogozhinë            legal form + diacritic
    Vikingur Reykjavik      KF Víkingur                      diacritic + dropped city
    Omonia Nicosia          AS Omónoia Leukosías             diacritic + TRANSLITERATION + exonym
    Kairat                  Qairat FK                        TRANSLITERATION (K/Q)

The previous matcher tokenised with ``re.findall(r"[a-z0-9]+", name.lower())`` and required EVERY
token of the shorter name to hit the longer. That fails three ways, all seen live:

  1. NO DIACRITIC FOLDING — the regex is ASCII-only, so an accent SPLITS the token:
     'Víkingur' -> ['v','kingur'], 'Omónoia' -> ['om','noia'], 'Górnik' -> ['g','rnik'].
     Worse, the stray one-letter 'v' then matched 'vikingur' by prefix and CONSUMED the slot,
     so the real token had nothing left to match.
  2. NO FUZZY — 'kairat' vs 'qairat' is one substitution and was a hard miss.
  3. ALL-TOKENS-MUST-MATCH — a sponsor ('Puntigamer') or city ('San Sebastian') present on one side
     only vetoed an otherwise certain match.

THE RULE HERE: fold to ASCII, drop legal-form and single-letter noise, then SCORE the significant
tokens. A pair is accepted when the evidence is strong (an exact/near token hit on both sides), and
an extra unmatched sponsor/city token on ONE side cannot veto it. Decoys still fail: 'Dinamo Zagreb'
vs 'Dinamo Bucuresti' share only the generic club word 'dinamo', which is not sufficient alone.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

# Legal forms / club-type prefixes and suffixes carried by one venue only. Dropping them is safe:
# they identify the *kind* of entity, never *which* club. Sourced from the live payloads above plus
# the standard European set.
# PURE legal forms — they name the KIND of entity, never WHICH club, so dropping them is lossless.
# Deliberately EXCLUDES club words that merely look generic ('real', 'atletico', 'sporting',
# 'athletic', 'deportivo'): stripping those turned 'Atletico' vs 'Atletico Madrid' into ''/'madrid'
# — a zero-token wipeout. Those belong in GENERIC_TOKENS, which weakens them without deleting them.
LEGAL_TOKENS = frozenset({
    "fc", "fk", "sk", "ac", "as", "sc", "cf", "cd", "ca", "cs", "nk", "kf", "bk", "if", "ks",
    "kks", "gnk", "hnk", "sv", "sd", "ud", "rc", "rcd", "afc", "cfc", "ffc", "vfb", "vfl",
    "fsv", "tsv", "tsg", "spvgg", "bsc", "msv", "ssv", "sgs", "us", "usc", "ss", "ssc", "asd",
    "cp", "club", "clube", "nogometni", "fotbalovy", "futbol", "futebol", "calcio",
    "spor", "kulubu", "koninklijke", "mh",
})
# Club-GENERIC words: shared by many DIFFERENT clubs, so a match on one of these alone is not
# identity ('Dinamo Zagreb' vs 'Dinamo Bucuresti'; 'Sporting CP' vs 'Sporting Gijon'). They are kept
# as tokens (they must still be COVERED) but do not count as identifying evidence.
GENERIC_TOKENS = frozenset({
    "dinamo", "dynamo", "united", "city", "town", "rovers", "wanderers", "albion", "county",
    "athletic", "atletico", "atletic", "real", "sporting", "olympic", "olympique", "inter",
    "internacional", "national", "sport", "sports", "spartak", "lokomotiv", "zenit",
    "deportivo", "royal", "hapoel", "maccabi", "estrella", "roja", "red", "green", "blue",
})
# A RESERVE/YOUTH marker on ONE side only means it is a DIFFERENT TEAM ('GNK Dinamo Zagreb' vs
# 'GNK Dinamo Zagreb II'). Presence asymmetry is an outright veto, never a tolerated extra token.
RESERVE_TOKENS = frozenset({
    "ii", "iii", "b", "c", "u19", "u20", "u21", "u23", "reserves", "reserve", "youth",
    "academy", "juniors", "junior", "amateure", "castilla", "atletic b",
})

_ALNUM_RE = re.compile(r"[a-z0-9]+")

# Latin letters whose NFKD decomposition does NOT strip to the expected ASCII (no combining mark).
_TRANSLIT = {
    "ß": "ss", "æ": "ae", "ø": "o", "å": "a", "đ": "d", "ð": "d", "þ": "th",
    "ł": "l", "ħ": "h", "ı": "i", "œ": "oe", "ʻ": "", "'": "", "`": "", "’": "",
}


def fold(text: str) -> str:
    """Fold a club name to lowercase ASCII: transliterate the letters NFKD cannot decompose, strip
    combining marks, drop apostrophes. 'Omónoia Leukosías' -> 'omonoia leukosias';
    'Bodoe/Glimt' -> 'bodoe glimt'; 'Be`er Sheva' -> 'beer sheva'."""
    s = str(text or "").lower()
    for a, b in _TRANSLIT.items():
        s = s.replace(a, b)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.split())


def tokens(name: str) -> list[str]:
    """Every ASCII-folded alphanumeric token of a club name (noise included).

    Runs of consecutive SINGLE letters are glued back together, because punctuation splits an
    initialism that both venues mean as one word: 'D.C. United' folds to 'd c united' and must
    tokenise as ['dc','united'] to line up with Kalshi's 'DC United'. A lone single letter (Kalshi's
    truncation marker, 'Los Angeles G') has no neighbour to glue to and is left alone."""
    raw = _ALNUM_RE.findall(fold(name))
    out: list[str] = []
    for t in raw:
        if len(t) == 1 and out and len(out[-1]) == 1:
            out[-1] += t
        else:
            out.append(t)
    return out


def significant(name: str) -> list[str]:
    """The tokens that actually identify a club: folded, with legal-form tokens and bare years/numbers
    removed. 'SK Puntigamer Sturm Graz' -> ['puntigamer','sturm','graz']; 'KF Víkingur' -> ['vikingur'];
    'Dos Hermanas CF 1971' -> ['dos','hermanas'].

    SINGLE LETTERS ARE KEPT. They are Kalshi TRUNCATION markers, not noise: 'Los Angeles G' is Galaxy
    and 'Los Angeles F(C)' is LAFC. Dropping them collapses both to ['los','angeles'] and pairs two
    different clubs — the exact collision the old prefix matcher was written to avoid. A single letter
    is matched by prefix in ``token_similar``, so it still lines up with the full word.

    Falls back to the raw tokens when stripping would leave nothing (a club literally named by its
    legal form, e.g. 'KÍ'), so a name can never normalize to the empty set."""
    out = [t for t in tokens(name) if t not in LEGAL_TOKENS and not t.isdigit()]
    return out or [t for t in tokens(name) if t] or []


def has_reserve_marker(name: str) -> bool:
    """The name denotes a RESERVE/YOUTH side ('… II', '… U21', '… Castilla')."""
    return any(t in RESERVE_TOKENS for t in tokens(name))


def token_similar(a: str, b: str) -> bool:
    """Two tokens denote the same word: equal; a TRUNCATION either way (one is a prefix of the other —
    'g'/'galaxy', 'pozna'/'poznan'); or ONE EDIT apart for words long enough that a single character
    cannot change identity (transliteration: qairat~kairat, omonoia~omonia).

    The one-edit rule needs a length floor: at 3 chars it would fuse 'psv' and 'psg'."""
    if a == b:
        return True
    if a.startswith(b) or b.startswith(a):
        return True
    if abs(len(a) - len(b)) <= 1 and max(len(a), len(b)) >= 5:
        return _within_one_edit(a, b)
    return False


def _initialism_span(short: str, rest: list[str]) -> Optional[list[int]]:
    """Indices in ``rest`` consumed when ``short`` is the INITIALISM of consecutive tokens there:
    'rb' -> ['red','bulls'] (New York RB / New York Red Bulls, verified live). Requires >= 2 letters
    so a single truncation letter can never swallow a whole word this way."""
    if len(short) < 2 or not short.isalpha():
        return None
    for start in range(len(rest)):
        span = rest[start:start + len(short)]
        if len(span) == len(short) and all(w[:1] == c for w, c in zip(span, short)):
            return list(range(start, start + len(short)))
    return None


def _within_one_edit(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` are at most ONE substitution/insertion/deletion apart."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if la == lb:                                            # one substitution
        return sum(1 for x, y in zip(a, b) if x != y) <= 1
    if abs(la - lb) != 1:
        return False
    short, long = (a, b) if la < lb else (b, a)             # one insertion
    i = j = 0
    skipped = False
    while i < len(short) and j < len(long):
        if short[i] == long[j]:
            i += 1
            j += 1
        elif skipped:
            return False
        else:
            skipped = True
            j += 1
    return True


def score(a: str, b: str) -> dict:
    """Score how strongly two club names denote the same club.

    Returns {score, strong, matched, a_tokens, b_tokens, unmatched_a, unmatched_b} where ``strong``
    counts matches on NON-generic tokens — the evidence that this is THIS club and not a namesake.
    ``score`` is the matched fraction of the SMALLER token set, so extra sponsor/city tokens on the
    longer side dilute nothing."""
    ta, tb = significant(a), significant(b)
    base = {"score": 0.0, "strong": 0, "matched": [], "a_tokens": ta, "b_tokens": tb,
            "unmatched_a": ta, "unmatched_b": tb, "reserve_mismatch": False}
    if not ta or not tb:
        return base
    if has_reserve_marker(a) != has_reserve_marker(b):        # first team vs its reserves -> VETO
        base["reserve_mismatch"] = True
        return base
    used: set[int] = set()
    matched: list[tuple[str, str]] = []
    unmatched_a: list[str] = []
    for t in ta:
        hit = next((i for i, u in enumerate(tb) if i not in used and token_similar(t, u)), None)
        if hit is None:
            unmatched_a.append(t)
            continue
        used.add(hit)
        matched.append((t, tb[hit]))
    # SECOND PASS: an unmatched short token may be the initialism of consecutive leftovers ('rb').
    for t in list(unmatched_a):
        free = [i for i in range(len(tb)) if i not in used]
        span = _initialism_span(t, [tb[i] for i in free])
        if span is not None:
            idx = [free[i] for i in span]
            used.update(idx)
            matched.append((t, " ".join(tb[i] for i in idx)))
            unmatched_a.remove(t)
    # THIRD PASS: a lone TRUNCATION LETTER may point at a token that was dropped as a legal form.
    # 'Los Angeles F' is LAFC, whose Poly name is 'Los Angeles FC' — the 'fc' we strip is exactly what
    # the 'f' abbreviates. Matching against the RAW tokens keeps that pair while still refusing
    # 'Los Angeles G' (Galaxy), whose 'g' matches nothing on the LAFC side.
    raw_b = [t for t in tokens(b) if t in LEGAL_TOKENS]
    for t in list(unmatched_a):
        if len(t) == 1 and any(u.startswith(t) for u in raw_b):
            matched.append((t, next(u for u in raw_b if u.startswith(t))))
            unmatched_a.remove(t)
    raw_a = [t for t in tokens(a) if t in LEGAL_TOKENS]
    leftover_b = [tb[i] for i in range(len(tb)) if i not in used]
    for t in list(leftover_b):
        if len(t) == 1 and any(u.startswith(t) for u in raw_a):
            used.add(tb.index(t))
            matched.append((next(u for u in raw_a if u.startswith(t)), t))
    strong = sum(1 for x, y in matched
                 if x not in GENERIC_TOKENS and y not in GENERIC_TOKENS and len(x) > 1)
    return {"score": len(matched) / float(min(len(ta), len(tb))),
            "strong": strong, "matched": matched, "a_tokens": ta, "b_tokens": tb,
            "unmatched_a": unmatched_a,
            "unmatched_b": [tb[i] for i in range(len(tb)) if i not in used],
            "reserve_mismatch": False}


# A pair is the same club when EVERY token of the smaller name matched (score 1.0) AND at least one
# of those matches was on a non-generic token. Requiring full coverage of the SMALLER side keeps
# decoys out ('Dinamo Zagreb' vs 'Dinamo Bucuresti' scores 0.5); requiring a strong token stops two
# clubs pairing on a shared generic word alone ('Sporting' vs 'Sporting').
ACCEPT_SCORE = 1.0
ACCEPT_STRONG = 1


def accepts(s: dict) -> bool:
    """Decide a scored pair. Beyond coverage + one identifying token, an UNMATCHED SINGLE LETTER on
    either side is a veto: Kalshi only ever emits one to DISAMBIGUATE ('Los Angeles G' = Galaxy,
    'Los Angeles F' = LAFC), so a leftover letter is positive evidence of a different club. Without
    this veto the score (normalised by the smaller token set) happily pairs LA Galaxy with LAFC."""
    if s.get("reserve_mismatch"):
        return False
    if any(len(t) == 1 for t in list(s["unmatched_a"]) + list(s["unmatched_b"])):
        return False
    return s["score"] >= ACCEPT_SCORE and s["strong"] >= ACCEPT_STRONG


def same_club(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` name the same club."""
    return accepts(score(a, b))


# --------------------------------------------------------------------------- #
# ALIAS map — the cases string rules CANNOT derive, and must not guess at        #
# --------------------------------------------------------------------------- #
# Some venue pairs differ by EXONYM or local-language name, where no amount of folding or fuzzing
# can connect them, and where guessing is dangerous because the near-miss shape is identical to a
# genuine decoy ('Sporting CP' vs 'Sporting Gijon'). These are recorded explicitly, keyed by folded
# name. Seeded from the 2026-07-28 live payloads; ``learn`` appends pairs confirmed at build time.
SEED_ALIASES: dict[str, str] = {
    # Omonia Nicosia (Kalshi) is AS Omónoia Leukosías (Poly) — 'Leukosia' IS 'Nicosia' in Greek.
    "omonia nicosia": "omonoia leukosias",
    # Kalshi abbreviates the Spanish giants to the bare club word; Poly carries the city.
    "atletico": "atletico madrid",
    "athletic": "athletic bilbao",
    # Milton Keynes Dons trade as 'MK Dons' on one side.
    "milton keynes": "mk dons",
}


def _alias_key(name: str) -> str:
    return " ".join(significant(name))


def same_club_with_alias(a: str, b: str, aliases: Optional[dict] = None) -> bool:
    """``same_club`` plus the alias map (seed + learned), checked in BOTH directions."""
    if same_club(a, b):
        return True
    table = dict(SEED_ALIASES)
    if aliases:
        table.update(aliases)
    ka, kb = _alias_key(a), _alias_key(b)
    for x, y in ((ka, kb), (kb, ka)):
        mapped = table.get(x)
        if mapped and (mapped == y or same_club(mapped, y)):
            return True
    return False


def learn(aliases: dict, a: str, b: str) -> dict:
    """Record a CONFIRMED pair so the next build is exact. Only stores what the rules could NOT
    derive on their own — a derivable pair would just bloat the file."""
    if not same_club(a, b):
        aliases[_alias_key(a)] = _alias_key(b)
    return aliases


def explain(a: str, b: str) -> str:
    """One-line diagnosis for the build log — the token sets on both sides and what matched, so a
    near-miss is self-diagnosing instead of a silent zero."""
    s = score(a, b)
    return (f"{a!r}->{b!r} score={s['score']:.2f} strong={s['strong']} "
            f"tokens={s['a_tokens']}/{s['b_tokens']} matched={[m[0] for m in s['matched']]} "
            f"unmatched={s['unmatched_a']}/{s['unmatched_b']}")


def best_match(name: str, candidates: list[str]) -> tuple[Optional[str], dict]:
    """The candidate that best denotes the same club as ``name``, with its score dict. Returns
    (None, best_score) when nothing clears the accept threshold."""
    best: tuple[Optional[str], dict] = (None, {"score": 0.0, "strong": 0, "matched": [],
                                               "a_tokens": significant(name), "b_tokens": [],
                                               "unmatched_a": [], "unmatched_b": []})
    for c in candidates:
        s = score(name, c)
        if s["score"] > best[1]["score"] or (s["score"] == best[1]["score"]
                                             and s["strong"] > best[1]["strong"]):
            best = (c, s)
    return best if (best[0] is not None and accepts(best[1])) else (None, best[1])
