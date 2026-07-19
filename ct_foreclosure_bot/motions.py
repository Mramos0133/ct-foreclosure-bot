"""Text patterns used to classify docket entries.

Patterns are matched against the plain-text description of a single docket
entry (e.g. "MOTION FOR JUDGMENT-STRICT FORECLOSURE RESULT: Order 1/10/2017
HON ANTONIO ROBAINA"). Real dockets observed on civilinquiry.jud.ct.gov use
"-" rather than "of" in these titles (e.g. "MOTION FOR JUDGMENT-STRICT
FORECLOSURE", not "MOTION FOR JUDGMENT OF STRICT FORECLOSURE"), so matching
is done on normalized substrings rather than the literal PB-title phrase.
"""

import re

# A case counts as a match if ANY of these fire on ANY docket entry.
# Each is (label, compiled regex). Regexes are matched against the
# uppercased, whitespace-normalized entry description.
TARGET_MOTION_PATTERNS = [
    ("Motion for Judgment of Strict Foreclosure",
     re.compile(r"MOTION\s+FOR\s+JUDGMENT.{0,20}STRICT\s+FORECLOSURE")),
    ("Motion for Judgment of Foreclosure by Sale",
     re.compile(r"MOTION\s+FOR\s+JUDGMENT.{0,20}FORECLOSURE\s+BY\s+SALE")),
]

DEFAULT_FAILURE_TO_APPEAR_PATTERN = re.compile(
    r"MOTION\s+FOR\s+DEFAULT.{0,20}FAILURE\s+TO\s+APPEAR"
)

FORECLOSURE_WORKSHEET_PATTERN = re.compile(r"FORECLOSURE\s+WORKSHEET")

# Bankruptcy-stay-then-reopened detection is two-part: a bankruptcy/stay
# entry, followed anywhere after it in the docket by an entry that
# re-establishes the case schedule. Observed real phrasing:
#   "AFFIDAVIT THAT PARTY IS IN BANKRUPTCY ... automatic stay in effect"
#   "MOTION TO RESET LAW DAYS AFTER FILING OF A BANKRUPTCY PETITION (CGS 49-15)"
# "Reopened"/"Reopening" is not a literal keyword that appears in practice,
# so this looks for bankruptcy + a subsequent reset/reopen-style motion
# rather than a literal "reopened" string.
BANKRUPTCY_PATTERN = re.compile(r"BANKRUPT")
STAY_PATTERN = re.compile(r"\bSTAY\b")
REOPEN_AFTER_BANKRUPTCY_PATTERNS = [
    re.compile(r"RESET\s+LAW\s+DAYS"),
    re.compile(r"MOTION\s+TO\s+OPEN\s+JUDGMENT"),
    re.compile(r"REOPEN"),
    re.compile(r"MOTION\s+TO\s+REOPEN"),
]


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.upper()).strip()


def find_target_motions(entry_text: str):
    """Return list of matched target-motion labels for one docket entry."""
    norm = normalize(entry_text)
    return [label for label, pattern in TARGET_MOTION_PATTERNS if pattern.search(norm)]


def is_failure_to_appear_default(entry_text: str) -> bool:
    return bool(DEFAULT_FAILURE_TO_APPEAR_PATTERN.search(normalize(entry_text)))


def is_foreclosure_worksheet(entry_text: str) -> bool:
    return bool(FORECLOSURE_WORKSHEET_PATTERN.search(normalize(entry_text)))


def is_bankruptcy_mention(entry_text: str) -> bool:
    return bool(BANKRUPTCY_PATTERN.search(normalize(entry_text)))


def is_reopen_after_bankruptcy(entry_text: str) -> bool:
    norm = normalize(entry_text)
    return any(p.search(norm) for p in REOPEN_AFTER_BANKRUPTCY_PATTERNS)


# "MOTION TO OPEN JUDGMENT AND EXTEND THE SALE DATE" is the observed real
# phrasing for a sale-date continuance (confirmed on a real docket, granted
# and denied instances both seen). Matched loosely (EXTEND + SALE DATE
# somewhere nearby) rather than the exact phrase, since law-day extensions
# on strict foreclosure cases may be worded slightly differently.
EXTEND_SALE_DATE_PATTERN = re.compile(r"EXTEND.{0,30}(SALE\s+DATE|LAW\s+DAY)")

GRANTED_PATTERN = re.compile(r"\bGRANTED\b")

# The document generated for a granted judgment motion is a separate docket
# entry, always literally titled "ORDER" (see order_document.py) -- but its
# numbering suffix relative to the motion entry is not consistent across
# cases (".86", ".10", sometimes a ".20" correction of an earlier ".10"),
# so it must be found by scanning nearby entries rather than assumed by a
# fixed offset.
ORDER_ENTRY_PATTERN = re.compile(r"^ORDER\b")


def is_extend_sale_date_or_law_day(entry_text: str) -> bool:
    return bool(EXTEND_SALE_DATE_PATTERN.search(normalize(entry_text)))


def is_granted(entry_text: str) -> bool:
    return bool(GRANTED_PATTERN.search(normalize(entry_text)))


def is_order_entry(entry_text: str) -> bool:
    return bool(ORDER_ENTRY_PATTERN.search(normalize(entry_text)))
