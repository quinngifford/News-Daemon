"""Stage 2 — disambiguation. Zero marginal cost, and where the precision lives.

Stage 1 tells you an alias and a death word share an item. That is a very weak
claim: "Trump mourns the death of a former aide" satisfies it perfectly. This
module turns raw matches into features that separate "the target died" from the
seven or so recurring false-positive classes documented in
docs/ARCHITECTURE.md §4.

The feature weights below are hand-set priors chosen to be defensible, not
optimal. Replace them with a trained model via tools/train_classifier.py once
tools/replay.py has produced a labelled set — the interface is the same
(features dict in, score out), so nothing downstream changes.
"""

from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass

from ticker.models import Match
from ticker.screen.automaton import (
    KIND_ALIAS,
    KIND_CONDOLENCE,
    KIND_DEATH,
    KIND_IDIOM,
)

# --- lexical patterns -------------------------------------------------------

# Genuine grammatical conditionals. "if X dies" is NEVER a report that X died,
# so this is close to dispositive and carries a heavy weight.
_CONDITIONAL = re.compile(
    r"\b(if|whether|would|could|should|might|may|were\s+to|in\s+the\s+event|"
    r"what\s+happens|what\s+would|hypothetical(ly)?|scenario|suppose|"
    r"wish(es|ed)?|imagine[sd]?|pretend(s|ed)?)\b",
    re.I,
)

# Topically adjacent to death but NOT conditional — these appear in real death
# coverage too ("Trump has died; succession questions follow"). Kept separate
# and weighted lightly on purpose: folding them into _CONDITIONAL would mean any
# strengthening of the conditional penalty starts killing true positives.
_SUCCESSION_TOPIC = re.compile(
    r"\b(succession|line\s+of\s+succession|contingency|contingencies|"
    r"prepare[sd]?\s+for|25th\s+amendment|next\s+in\s+line)\b",
    re.I,
)
_THREAT = re.compile(
    r"\b(threat(s|ened|ening)?|plot(s|ted|ting)?|assassination\s+attempt|attempt(ed)?\s+"
    r"(on|to\s+kill)|survived|wounded|shot\s+at|near\s+miss|foiled|averted|scare|hoax|"
    r"rumou?r(s|ed)?|false(ly)?|debunk(ed|s)?|denies?|denied)\b",
    re.I,
)
_METAPHOR_OBJ = re.compile(
    r"\b(\w+ism|campaign|presidency|candidacy|bid|hopes?|dreams?|legacy|brand|era|"
    r"movement|coalition|agenda|policy|bill|deal|talks?|momentum|poll(s|ing)?|"
    r"fund|funding|strategy|war|investigation|image|permit|licen[cs]e|stage|"
    r"sanctions?|treaty|ceasefire|truce|plan|proposal|effort|push|rally|"
    r"lawsuit|case|probe|tariffs?|programme?|project|venture)\b",
    re.I,
)

# A NEGATED death claim is strong counter-evidence. Only the negated forms are
# listed: bare "is dead" appears in genuine reports and must not be caught here.
_NEGATED_DEATH = re.compile(
    r"\b(isn'?t|wasn'?t|aren'?t|weren'?t|ain'?t|not|never|hardly|barely|no\s+longer)"
    r"\s+(?:\w+\s+){0,2}(dead|dying|died|deceased)\b",
    re.I,
)

# The author flagging their own figurative usage. Dispositive when present.
_EXPLICIT_METAPHOR = re.compile(
    r"\b(metaphorical(ly)?|figurative(ly)?|so\s+to\s+speak|"
    r"in\s+a\s+manner\s+of\s+speaking|proverbial(ly)?|politically\s+speaking)\b",
    re.I,
)

_QUOTES = "\"'“”‘’«»"
# "free beer WHEN Trump DIES" — future/conditional, not a report. Deliberately
# requires the death verb to FOLLOW the connective within a few words, so that
# "Trump died when he was 79" (connective after the verb) is unaffected.
_FUTURE_COND = re.compile(
    r"\b(when|whenever|if|after|until|before|should|the\s+day)\s+"
    r"(?:\w+[\s'’-]+){0,4}(dies|die|passes)\b",
    re.I,
)

# A generic human subject owns the death verb; the target is only a modifier.
# "California man ... dies", "Army veteran dies days after ..."
_GENERIC_SUBJECT = re.compile(
    r"\b(man|woman|men|women|veteran|soldier|officer|worker|workers|student|"
    r"teen|teenager|boy|girl|resident|residents|driver|passenger|passengers|"
    r"toddler|infant|mother|father|couple|victim|victims|inmate|suspect|"
    r"firefighter|deputy|marine|sailor|airman|pilot|hiker|climber|swimmer)\b",
    re.I,
)

_PAST_REF = re.compile(
    r"\b(anniversary|years?\s+ago|back\s+in\s+(19|20)\d\d|remembering|throwback|"
    r"on\s+this\s+day|archive|retrospective)\b",
    re.I,
)
_ATTRIBUTION = re.compile(
    r"\b(citing|according\s+to|per|as\s+reported\s+by|via|quoting|sources?\s+told)\s+"
    r"([A-Z][\w.&'-]*(?:\s+[A-Z][\w.&'-]*){0,3})",
)

_HONORIFICS = {
    "mr", "mrs", "ms", "miss", "dr", "sir", "lord", "lady", "rev", "sen", "senator",
    "rep", "gov", "governor", "president", "vice", "king", "queen", "prince",
    "princess", "pope", "judge", "justice", "gen", "general", "col", "capt", "sgt",
    "fmr", "former", "ex", "chief", "mayor", "secretary", "ambassador", "cardinal",
    "bishop", "actor", "actress", "singer", "author", "coach", "founder", "ceo",
}

# Capitalised things that are not people. Keeps "White House" from being read as
# a decedent whenever it sits next to a death word.
_NOT_PERSON = {
    "united states", "white house", "new york", "los angeles", "san francisco",
    "washington", "supreme court", "congress", "senate", "house", "republican",
    "republicans", "democrat", "democrats", "capitol", "pentagon", "kremlin",
    "middle east", "north korea", "south korea", "united kingdom", "great britain",
    "european union", "wall street", "social security", "world war", "civil war",
    "air force", "national guard", "west wing", "oval office", "mar a lago",
    "truth social", "fox news", "associated press", "new jersey", "north carolina",
    "west palm beach", "united nations", "state department", "justice department",
}

_CAP_SEQ = re.compile(r"\b[A-Z][a-z'’-]{1,20}(?:\s+[A-Z][a-z'’-]{1,20}){0,3}\b")

# A sentence end, or a headline joiner (" - ", " | ", " — "). Feeds routinely
# concatenate unrelated stories into one line:
#   "Takeaways from Trump's speech. And, at least 2 dead in Texas flooding"
# Token distance alone reads that as "Trump ... dead" and scored it 0.815 —
# above the fire threshold — on a live feed. Words either side of a boundary are
# not in the same statement and must not earn proximity credit.
_SENTENCE_BREAK = re.compile(r"[.!?][\s\"'’)\]]+[A-Z\"'“]|\s[-–—|]\s")


def _crosses_sentence(text: str, a: tuple[int, int], d: tuple[int, int]) -> bool:
    lo, hi = (a[1], d[0]) if a[1] <= d[0] else (d[1], a[0])
    if lo >= hi:
        return False
    return bool(_SENTENCE_BREAK.search(text[lo:hi]))

# Words that may legitimately precede a bare surname alias. Anything else
# capitalised in that slot is a DIFFERENT person who shares the surname —
# "Larry Trump", "George Trump", "Linda Marie Trump". These cannot be
# enumerated in config, so they are detected structurally instead.
_ALLOWED_BEFORE_SURNAME = {
    "president", "mr", "mrs", "ms", "miss", "dr", "sir", "lord", "lady",
    "former", "ex", "vice", "senator", "sen", "rep", "gov", "governor",
    "candidate", "nominee", "king", "queen", "prince", "princess", "pope",
    "the", "a", "an", "and", "or", "of", "by", "with", "for", "to", "from",
    "at", "on", "in", "as", "than", "vs", "anti", "pro", "said", "says",
    "told", "about", "after", "before", "when", "that", "how", "why",
    # Headline prefix labels. Without these, "Breaking: <Name> is dead" has its
    # alias suppressed as though "Breaking" were a given name — which silently
    # destroyed recall for every mononym target (found by tests/test_replay.py
    # on "Breaking: Pele is dead", scoring 0.000).
    "breaking", "exclusive", "update", "updated", "live", "watch", "opinion",
    "analysis", "report", "reports", "video", "photos", "developing", "alert",
    "just", "news", "world", "politics", "obituary", "profile",
}


# --- token/char index plumbing ---------------------------------------------


@dataclass(slots=True)
class _TokenIndex:
    """Maps character offsets to token offsets so distances are in words."""

    starts: list[int]

    @classmethod
    def build(cls, text: str) -> _TokenIndex:
        return cls([m.start() for m in re.finditer(r"\S+", text)])

    def token_of(self, char_idx: int) -> int:
        return max(0, bisect.bisect_right(self.starts, char_idx) - 1)


def _person_candidates(text: str, exclude: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Heuristic PERSON spans: capitalised multi-word names, or honorific + name.

    Deliberately not spaCy — a full NER model does not fit the memory budget of
    this box, and this recovers most of the value for the one question we ask of
    it ("is some *other* human named here?").
    """
    out: list[tuple[int, int]] = []
    for m in _CAP_SEQ.finditer(text):
        s, e = m.span()
        if any(s < xe and e > xs for xs, xe in exclude):
            continue  # overlaps the target's own alias
        phrase = m.group(0)
        low = phrase.lower()
        if low in _NOT_PERSON:
            continue
        words = phrase.split()
        first = words[0].lower().rstrip(".")
        # A single capitalised word is usually sentence-initial noise; require
        # either two words (First Last) or a preceding honorific.
        if len(words) >= 2 and first not in _NOT_PERSON or first in _HONORIFICS:
            out.append((s, e))
        else:
            prev = text[max(0, s - 14):s].strip().split()
            if prev and prev[-1].lower().rstrip(".") in _HONORIFICS:
                out.append((s, e))
    return out


# --- feature extraction -----------------------------------------------------


def foreign_given_name_spans(
    text: str, matches: list[Match], target_id: str, alias_terms: set[str]
) -> list[tuple[int, int]]:
    """Alias hits that are actually a DIFFERENT person sharing the surname.

    "Larry Trump Obituary", "George Trump Obituary", "Linda Marie Trump" — these
    cannot be enumerated in config the way "Fred Trump" can, because there are
    unboundedly many of them and local obituary feeds are full of them.

    Structural rule: a single-word alias preceded by a capitalised word is a
    different person, UNLESS that longer form is itself a configured alias
    ("Donald Trump", "President Trump") or the preceding word is a title.
    """
    out: list[tuple[int, int]] = []
    for m in matches:
        if m.kind != KIND_ALIAS or m.target_id != target_id:
            continue
        if " " in m.term.strip():
            continue                     # multi-word alias: already specific
        before = text[:m.start].rstrip()
        if not before:
            continue                     # start of text: no given name present
        tokens = before.split()
        # Walk back over middle initials so "Philip H. Trump" is judged on
        # "Philip", not on "H.". Without this the initial looks like a
        # non-name, the alias survives, and a stranger's obituary scores 0.883.
        # The same walk-back keeps "Donald J. Trump" working, since it resolves
        # to "Donald" and matches a configured alias.
        idx = len(tokens) - 1
        while idx >= 0 and re.fullmatch(r"[A-Z]\.?", tokens[idx]):
            idx -= 1
        if idx < 0:
            continue
        raw_prev = tokens[idx]
        # A token ending in ':' or '-' is a headline label or a hyphenated
        # modifier ("Breaking:", "Exclusive:", "pro-"), never a given name.
        if raw_prev.endswith((":", "-", "–", "—", "|")):
            continue
        prev = raw_prev.strip("\"'([{,.;!?)]}")
        # Require something that actually looks like a name: alphabetic and
        # capitalised. Being conservative here matters more than being thorough:
        # a false suppression silently destroys recall for the one event that
        # matters, while a missed collision only costs one LLM call.
        if len(prev) < 2 or not prev[:1].isupper() or not prev.replace(".", "").isalpha():
            continue
        if prev.lower() in _ALLOWED_BEFORE_SURNAME:
            continue
        if f"{prev.lower()} {m.term.lower()}" in alias_terms:
            continue                     # e.g. "donald trump" is a real alias
        out.append((m.start, m.end))
    return out


def extract_features(
    text: str,
    matches: list[Match],
    target_id: str,
    *,
    proximity_window: int = 12,
    strong_aliases: frozenset[str] = frozenset(),
    satire_source: bool = False,
) -> dict[str, float]:
    """Build the feature vector for one (item, target) pair.

    `text` must be the ORIGINAL-CASE text: capitalisation is signal here.
    """
    idx = _TokenIndex.build(text)
    alias_spans = [(m.start, m.end) for m in matches
                   if m.kind == KIND_ALIAS and m.target_id == target_id]
    death_spans = [(m.start, m.end) for m in matches if m.kind == KIND_DEATH]

    f: dict[str, float] = {}

    # --- proximity: does the death word plausibly attach to our target? ---
    best_dist = math.inf
    best_death: tuple[int, int] | None = None
    best_alias: tuple[int, int] | None = None
    any_pair = False
    for a in alias_spans:
        at = idx.token_of(a[0])
        for d in death_spans:
            any_pair = True
            # Only pairs inside the same statement earn proximity credit.
            if _crosses_sentence(text, a, d):
                continue
            dist = abs(idx.token_of(d[0]) - at)
            if dist < best_dist:
                best_dist, best_death, best_alias = dist, d, a

    # Every alias/death pair was split by a sentence break: the death word
    # belongs to a different statement entirely.
    f["cross_sentence"] = 1.0 if (any_pair and best_death is None) else 0.0
    if best_death is None and any_pair:
        # Fall back to the nearest pair purely so downstream features
        # (metaphor, quoting) still have a death span to inspect.
        for a in alias_spans:
            at = idx.token_of(a[0])
            for d in death_spans:
                dist = abs(idx.token_of(d[0]) - at)
                if dist < best_dist:
                    best_dist, best_death, best_alias = dist, d, a
    if f["cross_sentence"]:
        f["proximity"] = 0.15        # different statement: no real adjacency
    elif best_dist is math.inf:
        f["proximity"] = 0.0
    elif best_dist <= proximity_window:
        # linear decay inside the window: adjacent is much better than 12 apart
        f["proximity"] = 1.0 - (best_dist / (proximity_window + 1)) * 0.5
    else:
        f["proximity"] = 0.15

    # --- THE decisive feature: is a different human closer to the death word? ---
    people = _person_candidates(text, exclude=alias_spans)
    f["n_other_people"] = float(min(len(people), 5))
    f["other_person_nearer"] = 0.0
    if best_death is not None and people:
        dtok = idx.token_of(best_death[0])
        atok = idx.token_of(best_alias[0]) if best_alias else 10**6
        nearest_other = min(abs(idx.token_of(s) - dtok) for s, _ in people)
        if nearest_other < abs(atok - dtok):
            f["other_person_nearer"] = 1.0

    # --- negative context ---
    # --- appositive subject: "X, ally of TARGET, dies" ---
    # THE most dangerous false-positive class found in live data, because
    # proximity actively favours the wrong person: in "Lindsey Graham, key ally
    # of Donald Trump, dies", the target sits closer to the verb than the real
    # subject does. English headlines are SUBJECT-first, so a person named ahead
    # of our target — especially with the target inside a comma-delimited
    # appositive or a prepositional phrase — owns the verb.
    f["subject_is_other"] = 0.0
    if alias_spans and people:
        first_alias = min(s for s, _ in alias_spans)
        earlier = [(s, e) for s, e in people if s < first_alias]
        if earlier:
            # Target introduced by an appositive comma or a preposition ⇒ modifier.
            between = text[max(e for _, e in earlier):first_alias]
            if "," in between or re.search(
                r"\b(of|to|for|with|by|ally|allies|aide|adviser|advisor|critic|"
                r"friend|rival|appointee|nominee|pick|supporter|backer|donor|"
                r"lawyer|attorney|spokesman|spokeswoman)\b", between, re.I
            ):
                f["subject_is_other"] = 1.0
            elif min(s for s, _ in earlier) <= 2:
                # Headline-initial person with no linking word still outranks a
                # target mentioned later.
                f["subject_is_other"] = 1.0

    # A generic human subject ("California man ... dies") likewise owns the verb.
    f["generic_subject"] = 0.0
    if best_death is not None and _GENERIC_SUBJECT.search(text[:best_death[0]]):
        f["generic_subject"] = 1.0

    f["condolence"] = 1.0 if any(m.kind == KIND_CONDOLENCE for m in matches) else 0.0
    f["idiom"] = 1.0 if any(m.kind == KIND_IDIOM for m in matches) else 0.0
    f["conditional"] = 1.0 if _CONDITIONAL.search(text) else 0.0
    f["succession_topic"] = 1.0 if _SUCCESSION_TOPIC.search(text) else 0.0
    f["future_cond"] = 1.0 if _FUTURE_COND.search(text) else 0.0
    f["threat"] = 1.0 if _THREAT.search(text) else 0.0
    f["past_ref"] = 1.0 if _PAST_REF.search(text) else 0.0
    f["satire"] = 1.0 if satire_source else 0.0

    # metaphor: death word governing an abstraction rather than a person
    f["metaphor"] = 0.0
    f["quoted_death"] = 0.0
    if best_death is not None:
        tail = text[best_death[1]:best_death[1] + 40]
        head = text[max(0, best_death[0] - 25):best_death[0]]
        if _METAPHOR_OBJ.search(tail) or _METAPHOR_OBJ.search(head):
            f["metaphor"] = 1.0
        # Scare-quoted death term — "the fund is 'dead'" — is the author marking
        # the word as not-literal. Very reliable in practice.
        before = text[best_death[0] - 1] if best_death[0] > 0 else ""
        after = text[best_death[1]] if best_death[1] < len(text) else ""
        if before in _QUOTES and after in _QUOTES:
            f["quoted_death"] = 1.0

    f["negated_death"] = 1.0 if _NEGATED_DEATH.search(text) else 0.0
    f["explicit_metaphor"] = 1.0 if _EXPLICIT_METAPHOR.search(text) else 0.0

    # --- positive context ---
    matched_alias_terms = {
        m.term.lower() for m in matches
        if m.kind == KIND_ALIAS and m.target_id == target_id
    }
    f["strong_alias"] = 1.0 if matched_alias_terms & {a.lower() for a in strong_aliases} else 0.0
    # A death word in the headline is stronger evidence than one buried in body
    # text. Title-only items have no newline, so the whole text is headline.
    nl = text.find("\n")
    headline_end = len(text) if nl < 0 else nl
    f["in_headline"] = 1.0 if best_death and best_death[0] < headline_end else 0.0

    return f


# Hand-set priors. Signs matter more than magnitudes; retrain to tune.
WEIGHTS: dict[str, float] = {
    "proximity": 3.4,
    "strong_alias": 1.2,
    "in_headline": 0.6,
    "other_person_nearer": -4.2,
    "condolence": -3.6,
    # Heavy: must overcome proximity + strong_alias + in_headline (+5.07 at most)
    # so that "What happens if <Full Name> dies?" cannot reach the score floor.
    # The canary's negative/conditional probe is what guards this value.
    "conditional": -4.8,
    "succession_topic": -0.8,
    # Live data showed these two are decisive, and both must be able to drag a
    # score that has proximity + strong_alias + in_headline (+5.07) below the
    # 0.80 min_score that permits firing.
    # Compound/concatenated headlines. Found live at 0.815 — above the fire
    # threshold — on an NPR line that joined a Trump speech story to a Texas
    # flooding death toll.
    "cross_sentence": -3.0,
    "subject_is_other": -4.6,
    "generic_subject": -3.4,
    "future_cond": -4.2,
    "threat": -2.8,
    "idiom": -5.0,
    "metaphor": -2.5,
    "quoted_death": -3.2,
    "negated_death": -4.5,
    "explicit_metaphor": -5.0,
    "past_ref": -1.0,
    "n_other_people": -0.25,
    "satire": -8.0,
}
BIAS = -1.6


def score(features: dict[str, float]) -> float:
    """Logistic combination of features → probability-like score in (0, 1)."""
    z = BIAS + sum(WEIGHTS.get(k, 0.0) * v for k, v in features.items())
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def explain(features: dict[str, float]) -> str:
    """Human-readable contribution breakdown. Goes in the alert audit trail."""
    parts = sorted(
        ((k, WEIGHTS.get(k, 0.0) * v) for k, v in features.items() if v),
        key=lambda kv: -abs(kv[1]),
    )
    return ", ".join(f"{k}{c:+.2f}" for k, c in parts if abs(c) > 0.01)


_RETRACTION = re.compile(
    r"\b(retract(s|ed|ion)?|correction|corrects|clarification|"
    r"(was|is|are|were)\s+(false|untrue|incorrect|a\s+hoax|inaccurate)|"
    r"we\s+regret|misreport(ed|ing)?|debunk(ed|s)?|"
    r"(denies?|denied|disputes?)\s+(the\s+)?(report|claim)|"
    r"no\s+evidence|hoax|not\s+true|still\s+alive|alive\s+and\s+well)\b",
    re.I,
)


def is_retraction(text: str) -> bool:
    """Does this item walk back a death claim?

    Used in TWO places, deliberately:
      * screen/funnel.py — to keep retractions ALIVE regardless of their score.
        A retraction looks almost exactly like a false positive to the rules
        above ("was false" trips the threat pattern), so the very logic that
        protects against bad fires would suppress the correction to a bad fire.
        A false CONFIRMED that can never be walked back is the worst outcome
        this system can produce.
      * confirm/policy.py — to mark the evidence negative so it subtracts.
    """
    return bool(_RETRACTION.search(text))


def find_attribution(text: str) -> str | None:
    """Extract 'citing AP' / 'according to Reuters' → the true origin.

    Without this, one rumour echoed by 30 outlets looks like 30 independent
    confirmations. See docs/ARCHITECTURE.md §5.
    """
    m = _ATTRIBUTION.search(text)
    return m.group(2).strip() if m else None
