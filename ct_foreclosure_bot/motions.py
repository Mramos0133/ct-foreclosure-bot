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
#
# "Law Day" and "Motion to Open Judgment" were added on top of the original
# two judgment motions per explicit request, to widen the net: a case can
# carry a Law Day (strict foreclosure) or a Motion to Open Judgment
# (typically a case being reopened/continued) without necessarily also
# having the exact original motion-title wording matched above -- e.g. a
# docket entry like "MOTION TO RESET LAW DAYS AFTER FILING OF A BANKRUPTCY
# PETITION" carries "LAW DAY" but wouldn't match either pattern above.
TARGET_MOTION_PATTERNS = [
    ("Motion for Judgment of Strict Foreclosure",
     re.compile(r"MOTION\s+FOR\s+JUDGMENT.{0,20}STRICT\s+FORECLOSURE")),
    ("Motion for Judgment of Foreclosure by Sale",
     re.compile(r"MOTION\s+FOR\s+JUDGMENT.{0,20}FORECLOSURE\s+BY\s+SALE")),
    ("Law Day",
     re.compile(r"LAW\s+DAYS?")),
    ("Motion to Open Judgment",
     re.compile(r"MOTION\s+TO\s+OPEN\s+JUDGMENT")),
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


# EMAP-or-loan-modification-then-failed detection, same two-part shape as
# the bankruptcy-then-reopened detection above: a docket entry showing the
# owner entered the program, followed anywhere after it by an entry that
# signals the program didn't stick and the foreclosure/auction is moving
# again.
#
# These patterns WERE a best-effort guess; they have since been checked
# against every MEDIAT/EMAP entry across 360 real Milford/Bridgeport/
# Stratford dockets, and the guess was wrong in both directions:
#
#   - Over-matching. CT files mediation paperwork on essentially every
#     foreclosure, so a bare "FORECLOSURE MEDIATION" test fired on ~197
#     of 360 dockets. The worst of it: "FORECLOSURE MEDIATION - INELIGIBLE
#     CASE" (158 hits) counted as *entering* a program the case was
#     explicitly ruled ineligible for, and "AFFIDAVIT OF COMPLIANCE WITH
#     EMAP" (96 hits) -- the lender attesting it sent the required EMAP
#     notice -- counted as the owner entering EMAP.
#   - Under-matching the ending. The real termination entry is
#     "FORECLOSURE MEDIATION TIME PERIOD EXPIRED" (138 hits); the old
#     pattern only recognized "MEDIATION PERIOD TERMINATED" (30 hits),
#     missing 82% of real terminations.
#
# Entry now requires an affirmative opt-in or assignment; the routine
# administrative and compliance filings are excluded explicitly.
EMAP_PATTERN = re.compile(r"\bEMAP\b|EMERGENCY\s+MORTGAGE\s+ASSISTANCE")
LOAN_MODIFICATION_PROGRAM_PATTERN = re.compile(
    r"LOAN\s+MODIFICATION"
    r"|LOSS\s+MITIGATION"
    r"|MEDIATION\s+REQUEST"
    r"|REQUEST/CERTIFICATE"
    r"|JD-CV-108"
    r"|ORDER\s+ASSIGNED\s+TO\s+(PRE)?MEDIATION"
)
# Administrative/compliance chatter that mentions a program without the
# owner having entered one -- checked before the entry patterns above.
PROGRAM_ENTRY_EXCLUDES = re.compile(
    r"INELIGIBLE"
    r"|COMPLIANCE\s+WITH"
    r"|AFFIDAVIT\s+OF\s+COMPLIANCE"
    r"|ELIGIBLE\s+CASE"          # auto-generated eligibility flag, not an opt-in
    r"|OBJECTION"
    r"|\bMOTION\s+TO\s+EXTEND\b"
    r"|MODIFICATION\s+OF\s+MEDIATION\s+PERIOD"  # JD-CV-96 scheduling motion, not a loan mod
)

# Program-failure signals beyond those already covered by
# find_target_motions() (Motion for Judgment-Strict/By Sale, Motion to
# Open Judgment, Law Day) and is_reopen_after_bankruptcy() (Reset Law
# Days / Reopen) -- both reused below, since "the case is moving toward
# foreclosure/auction again" means the same thing regardless of which
# kind of stay preceded it. The two MEDIATION_ENDED_PATTERN forms are the
# exact strings CT uses; see the note above.
MEDIATION_ENDED_PATTERN = re.compile(
    r"MEDIATION\s+TIME\s+PERIOD\s+EXPIRED"
    r"|MEDIATION\s+PERIOD\s+(TERMINATED|EXPIRED|ENDED|CLOSED)"
)
PROGRAM_FAILURE_PATTERN = re.compile(
    r"MOTION\s+TO\s+DENY"
    r"|\bDENIED\b"
    r"|MOTION\s+TO\s+RESUME"
    r"|MOTION\s+TO\s+REACTIVATE"
    r"|EMAP\s+(APPLICATION\s+)?DENIED"
    r"|MEDIATION\s+TIME\s+PERIOD\s+EXPIRED"
    r"|MEDIATION\s+PERIOD\s+(TERMINATED|EXPIRED|ENDED|CLOSED)"
)


def is_mediation_ended(entry_text: str) -> bool:
    """True for the two entries CT actually files when the mediation
    period stops: "FORECLOSURE MEDIATION TIME PERIOD EXPIRED" and
    "FORECLOSURE MEDIATOR'S FINAL REPORT - MEDIATION PERIOD TERMINATED".
    """
    return bool(MEDIATION_ENDED_PATTERN.search(normalize(entry_text)))


# Recent-lender-complaint detection (see lead_ranking.py for the HOT rule
# built on these). Two independent tests:
#   1. The docket has a COMPLAINT entry (the case-initiating pleading).
#      The earliest matching entry is used; the excludes keep later
#      entries that merely *reference* the complaint (answers, requests
#      to revise, motions to strike/dismiss it) from being mistaken for
#      it if the docket somehow lacks the original.
#   2. The plaintiff (caption text before "v.") reads like a bank/lender,
#      not a condo association, municipality/tax collector, or private
#      individual. Keyword-based; "NATIONAL ASSOCIATION"/"AS TRUSTEE"
#      cover the securitized-trust phrasings (e.g. "U.S. BANK NATIONAL
#      ASSOCIATION, AS TRUSTEE FOR ...").
COMPLAINT_ENTRY_PATTERN = re.compile(r"\bCOMPLAINT\b")
COMPLAINT_ENTRY_EXCLUDES = re.compile(
    r"\bANSWER\b|REQUEST\s+TO\s+REVISE|MOTION\s+TO\s+STRIKE|MOTION\s+TO\s+DISMISS"
    r"|OBJECTION|\bREPLY\b|\bRESPONSE\b"
)
LENDER_PLAINTIFF_PATTERN = re.compile(
    r"\bBANK\b|MORTGAGE|\bLOANS?\b|LENDING|LENDER|FINANC"  # FINANCE / FINANCIAL
    r"|CREDIT\s+UNION|\bFCU\b|SAVINGS|FUNDING|SERVICING"
    r"|NATIONAL\s+ASSOCIATION|AS\s+TRUSTEE|TRUST\s+COMPANY|HOME\s+EQUITY"
)


def is_complaint_entry(entry_text: str) -> bool:
    norm = normalize(entry_text)
    return bool(COMPLAINT_ENTRY_PATTERN.search(norm)) and not COMPLAINT_ENTRY_EXCLUDES.search(norm)


def is_lender_plaintiff(case_caption: str) -> bool:
    plaintiff_part = re.split(r"\bvs?\.?\s", normalize(case_caption), maxsplit=1)[0]
    return bool(LENDER_PLAINTIFF_PATTERN.search(plaintiff_part))


def program_entry_label(entry_text: str) -> str | None:
    """"EMAP" | "Foreclosure Mediation" | "Loan Modification" | None for
    one docket entry. Administrative/compliance filings that merely
    mention a program are excluded first -- see PROGRAM_ENTRY_EXCLUDES.
    """
    norm = normalize(entry_text)
    if PROGRAM_ENTRY_EXCLUDES.search(norm):
        return None
    if EMAP_PATTERN.search(norm):
        return "EMAP"
    if LOAN_MODIFICATION_PROGRAM_PATTERN.search(norm):
        # Mediation and a servicer loan-mod are different programs and
        # read differently on a call; don't collapse them into one label.
        if "MEDIATION" in norm:
            return "Foreclosure Mediation"
        return "Loan Modification"
    return None


def is_assistance_program_entry(entry_text: str) -> bool:
    return program_entry_label(entry_text) is not None


def is_program_failure_motion(entry_text: str) -> bool:
    if find_target_motions(entry_text) or is_reopen_after_bankruptcy(entry_text):
        return True
    return bool(PROGRAM_FAILURE_PATTERN.search(normalize(entry_text)))


# "MOTION TO OPEN JUDGMENT AND EXTEND THE SALE DATE" is the observed real
# phrasing for a sale-date continuance (confirmed on a real docket, granted
# and denied instances both seen). Matched loosely (EXTEND + SALE DATE
# somewhere nearby) rather than the exact phrase, since law-day extensions
# on strict foreclosure cases may be worded slightly differently.
EXTEND_SALE_DATE_PATTERN = re.compile(r"EXTEND.{0,30}(SALE\s+DATE|LAW\s+DAY)")

GRANTED_PATTERN = re.compile(r"\bGRANTED\b")

# Contact-tracing signals: these entry types are where an owner's actual
# current address (as opposed to the property address, which may no longer
# be occupied by them) or a new decision-maker (an heir/estate rep/trustee
# who has replaced the original owner as the party who controls the
# property) tend to surface. Confirmed against real dockets: "RETURN OF
# SERVICE" (marshal's return, entry no. always "100.30" in practice) and
# "APPEARANCE" (JD-CL-12, a fixed state form) are present on essentially
# every case; "MOTION TO SUBSTITUTE PARTY" entry text is generic and does
# NOT itself say which side (plaintiff or defendant) is being substituted
# -- confirmed on a real filing where it turned out to be a plaintiff-side
# lender substitution, unrelated to the owner -- so this is a "worth a
# look" signal, not a confirmed one.
RETURN_OF_SERVICE_PATTERN = re.compile(r"RETURN\s+OF\s+SERVICE|SHERIFF'?S?\s+RETURN")
APPEARANCE_ENTRY_PATTERN = re.compile(r"^APPEARANCE\b")
SUBSTITUTE_PARTY_PATTERN = re.compile(r"MOTION\s+TO\s+SUBSTITUTE\s+PARTY")

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


def is_return_of_service(entry_text: str) -> bool:
    return bool(RETURN_OF_SERVICE_PATTERN.search(normalize(entry_text)))


def is_appearance_entry(entry_text: str) -> bool:
    return bool(APPEARANCE_ENTRY_PATTERN.search(normalize(entry_text)))


def is_substitute_party_motion(entry_text: str) -> bool:
    return bool(SUBSTITUTE_PARTY_PATTERN.search(normalize(entry_text)))
