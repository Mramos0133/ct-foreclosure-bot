"""Detects whether a foreclosure case involves a deceased owner's estate,
and pulls out who the heirs/fiduciary are.

Why it matters operationally: if the owner is dead, the person who can
actually sell the house is a fiduciary (administrator/executor) or the
heirs -- not "the owner". Knocking or calling without knowing that wastes
the contact and lands badly. These cases also move differently: a sale
usually needs Probate Court involvement, which is both a delay and a
negotiating reality worth knowing before the first conversation.

Two independent sources, because neither is reliable alone:

  1. The case caption / party names. CT captions name the fiduciary
     explicitly -- "ZALEWSKI, LAUREN, ADMINISTRSTRIX OF THE ESTATE OF ..."
     (sic; the court's own typo, which is why the pattern is loose), or
     name the class generically -- "THE WIDOW, HEIRS, BENEFICIARIES,
     REPRESENTATIVES AND CREDITORS OF ...". Both were seen in real data.
  2. Docket entry text, which mentions Probate Court, an estate fiduciary
     certificate, or a suggestion of death when the owner died mid-case
     (the caption then still names the living owner and source 1 misses
     it entirely).

Heir NAMES are best-effort. A caption that says "the heirs of John Smith"
names nobody; the individual heirs, when identified at all, appear as
additional defendants or inside the complaint's party paragraphs. What
this module guarantees is the flag and the fiduciary's name when the
caption carries it -- treat a populated heir list as a lead worth
verifying against the source document, not a settled answer.
"""

import re

from .models import DocketEntry

# Fiduciary role words, deliberately prefix-matched: real captions contain
# "ADMINISTRSTRIX", "ADMINISTRATRIX", "ADMINISTRATOR", "EXECUTRIX",
# "EXECUTOR", and the court does not spell them consistently.
FIDUCIARY_ROLE = re.compile(
    r"\bADMINIS\w*|\bEXECUT(?:OR|RIX|RICE)\w*|\bFIDUCIARY\b|\bCONSERVATOR\w*", re.I
)
ESTATE_OF = re.compile(r"ESTATE\s+OF\s+(.+?)(?:\s*,?\s*ET\s+AL\.?|\s*$)", re.I)
HEIR_CLASS = re.compile(
    r"\b(?:WIDOW|WIDOWER|HEIRS?|BENEFICIAR\w+|DEVISEES?|LEGATEES?|"
    r"NEXT\s+OF\s+KIN|REPRESENTATIVES)\b", re.I
)
DECEASED = re.compile(r"\bDECEASED\b|\bDEC'?D\b|\bLATE\s+OF\b", re.I)

# Docket-entry signals. Kept separate from the caption patterns because
# these catch the owner-died-mid-case shape the caption cannot.
PROBATE_ENTRY = re.compile(
    r"PROBATE\s+COURT"
    r"|SUGGESTION\s+OF\s+DEATH"
    r"|CERTIFICATE\s+OF\s+DEVISE"
    r"|FIDUCIARY(?:'S)?\s+(?:CERTIFICATE|PROBATE)"
    r"|APPOINTMENT\s+OF\s+(?:ADMINISTRATOR|EXECUTOR|FIDUCIARY)"
    r"|ESTATE\s+OF\b"
    r"|MOTION\s+TO\s+SUBSTITUTE.{0,40}(?:ADMINISTRAT|EXECUT|ESTATE|HEIR)",
    re.I,
)

_SPLIT_PARTIES = re.compile(r"\bvs?\.\s+", re.I)
_TRAILING_ET_AL = re.compile(r"\s*,?\s*ET\s*AL\.?\s*$", re.I)


def _defendant_side(case_caption: str) -> str:
    parts = _SPLIT_PARTIES.split(case_caption, maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else case_caption.strip()


def is_probate_case(case_caption: str, entries: list[DocketEntry] | None = None) -> bool:
    """True when the case involves a deceased owner's estate, judged from
    the caption and (when given) the docket entries.
    """
    defendant = _defendant_side(case_caption)
    if (
        FIDUCIARY_ROLE.search(defendant)
        or ESTATE_OF.search(defendant)
        or HEIR_CLASS.search(defendant)
        or DECEASED.search(defendant)
    ):
        return True
    if entries:
        return any(PROBATE_ENTRY.search(e.description or "") for e in entries)
    return False


def probate_signal(case_caption: str, entries: list[DocketEntry] | None = None) -> str:
    """Short human-readable reason the case was flagged, for the sheet.
    Empty string when not a probate case.
    """
    defendant = _defendant_side(case_caption)
    if FIDUCIARY_ROLE.search(defendant):
        return "Fiduciary named in case caption"
    if ESTATE_OF.search(defendant):
        return "Estate named in case caption"
    if HEIR_CLASS.search(defendant):
        return "Heirs/beneficiaries named as defendants"
    if DECEASED.search(defendant):
        return "Owner shown as deceased in caption"
    if entries:
        for e in entries:
            if PROBATE_ENTRY.search(e.description or ""):
                desc = re.sub(r"\s+", " ", e.description).strip()
                return f"Probate activity on docket: {desc[:70]}"
    return ""


def extract_estate_of(case_caption: str) -> str:
    """The deceased owner's name, when the caption says "ESTATE OF X"."""
    m = ESTATE_OF.search(_defendant_side(case_caption))
    if not m:
        return ""
    name = _TRAILING_ET_AL.sub("", m.group(1)).strip(" ,")
    return re.sub(r"\s+", " ", name)


def extract_heirs(case_caption: str, entries: list[DocketEntry] | None = None) -> list[str]:
    """Best-effort list of the people to actually talk to: the named
    fiduciary first (they hold authority to sell), then any individually
    named heir-side defendants.

    Returns [] rather than guessing when the caption only names the heir
    class generically ("THE WIDOW, HEIRS, ... OF JOHN SMITH") -- there is
    no name in that text to extract, and inventing one would be worse than
    an empty cell.
    """
    defendant = _defendant_side(case_caption)
    out: list[str] = []

    # "ZALEWSKI, LAUREN, ADMINISTRSTRIX OF THE ESTATE OF LAURA X"
    # -> the fiduciary is everything before the role word.
    role = FIDUCIARY_ROLE.search(defendant)
    if role:
        before = defendant[: role.start()].strip(" ,")
        before = _TRAILING_ET_AL.sub("", before).strip(" ,")
        if before and not HEIR_CLASS.search(before):
            parts = [p.strip() for p in before.split(",") if p.strip()]
            if len(parts) >= 2:
                name = f"{parts[1]} {parts[0]}"          # "LAST, FIRST" -> "FIRST LAST"
            else:
                name = before
            label = re.sub(r"\s+", " ", name).title()
            out.append(f"{label} (fiduciary)")

    if entries:
        for e in entries:
            desc = e.description or ""
            if re.search(r"MOTION\s+TO\s+SUBSTITUTE", desc, re.I) and re.search(
                r"ADMINISTRAT|EXECUT|ESTATE|HEIR", desc, re.I
            ):
                cleaned = re.sub(r"\s+", " ", desc).strip()
                out.append(f"see substitution filing: {cleaned[:60]}")
                break

    return out
