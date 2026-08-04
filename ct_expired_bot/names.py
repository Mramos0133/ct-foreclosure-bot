"""Splitting an assessor owner name into first/last for the vendor CSV.

WHICH TOKEN IS THE SURNAME: assessor cards print `LAST FIRST [MIDDLE]`
with no comma, and that is the default this module assumes. It is not an
inference from the spec -- the spec anticipated `FIRST LAST` -- it is
what the source actually prints. Sampled live from Vision/Stamford on
2026-08-04:

    'CARTWRIGHT ANGELA'      'ALI MOHAMMED'        'CORREA ELIZABETH'
    'MELECIO JAMIE K'        'COMERFORD ELAINE P'  'CAIRO MATTHEW P'
    'AGURCIA REINALDO'       'GOULD RICHARD ET AL' 'HLEBOGIANNIS MARIA ET AL'

Reading those as `FIRST LAST` would send every single one to the vendor
reversed -- "Angela Cartwright" billed as first name CARTWRIGHT -- which
is exactly the failure this bot exists to prevent. So `split_owner_name`
takes an explicit `order`, and the assessor path passes the format its
source actually uses rather than relying on a default that happens to be
right.

Beyond ordering, the spec is deliberately narrow: split *only* when the
printed name is unambiguous. Trusts, LLCs, estates, and multiple owners
go through whole, in the last-name field, first name blank.

The bias here is one-directional on purpose. Refusing to split a name
that could have been split costs nothing -- the vendor still gets the
full string and matches on it. Splitting a name that should not have been
split ("SMITH FAMILY TRUST" -> first "SMITH", last "FAMILY TRUST") sends
a person who does not exist to the skip trace vendor and bills for it.
So every ambiguous case resolves to "don't split".

Cases that intentionally do NOT split:
  - anything containing an entity keyword (LLC, TRUST, ESTATE OF, ...)
  - anything joining two people ("&", " AND ", multiple commas)
  - 3+ token names where the middle token is not a single initial
    ("MARY VAN DER BERG" -- is the surname "BERG" or "VAN DER BERG"?)
  - a hyphenated or particle surname is fine as the last token; it is the
    *middle* of the name that creates the ambiguity
"""

import re

# Substrings that make a name an organization/trust/estate rather than a
# person. Matched on word boundaries against the upper-cased name.
ENTITY_KEYWORDS = [
    "LLC", "L L C", "INC", "CORP", "CORPORATION", "COMPANY", "CO",
    "TRUST", "TRUSTEE", "TRUSTEES", "TR", "REVOCABLE", "IRREVOCABLE",
    "ESTATE", "ESTATES", "EST OF", "HEIRS", "LIFE USE", "LIFE ESTATE",
    "LP", "LLP", "LTD", "PARTNERSHIP", "PARTNERS", "ASSOCIATES",
    "ASSOCIATION", "BANK", "NA", "N A", "FEDERAL", "MORTGAGE",
    "PROPERTIES", "PROPERTY", "REALTY", "HOLDINGS", "INVESTMENTS",
    "ENTERPRISES", "DEVELOPMENT", "BUILDERS", "CONSTRUCTION",
    "FOUNDATION", "CHURCH", "MINISTRIES", "AUTHORITY", "COMMISSION",
    "HOUSING", "TOWN OF", "CITY OF", "STATE OF", "DEPT", "DEPARTMENT",
    "NOMINEE", "CUSTODIAN", "FBO", "IRA", "ET AL", "ETAL", "ET UX",
]

# Generational/honorific suffixes that ride along with the surname.
SUFFIXES = {"JR", "SR", "II", "III", "IV", "V", "JR.", "SR."}


def _normalize(raw: str) -> str:
    return re.sub(r"\s+", " ", (raw or "").strip())


def is_entity(name: str) -> bool:
    """True if the name looks like an org/trust/estate rather than a person."""
    upper = _normalize(name).upper()
    if not upper:
        return False
    # Strip punctuation to word-boundary tokens so "SMITH, TRUSTEE" and
    # "ABC L.L.C." both hit.
    tokens = set(re.findall(r"[A-Z&]+", upper))
    for keyword in ENTITY_KEYWORDS:
        parts = keyword.split()
        if len(parts) == 1:
            if parts[0] in tokens:
                return True
        elif re.search(rf"\b{re.escape(keyword)}\b", upper):
            return True
    return False


def is_multiple_owners(name: str) -> bool:
    """True if the string names more than one party."""
    normalized = _normalize(name)
    if not normalized:
        return False
    if "&" in normalized or "+" in normalized:
        return True
    if re.search(r"\bAND\b", normalized.upper()):
        return True
    if normalized.count(",") > 1:
        return True
    if ";" in normalized or "/" in normalized:
        return True
    return False


def _is_initial(token: str) -> bool:
    stripped = token.replace(".", "")
    return len(stripped) == 1 and stripped.isalpha()


def _split_last_first(normalized: str) -> tuple[str, str] | None:
    """`LAST, FIRST` / `LAST, FIRST M` -> (first, last). None if ambiguous."""
    last_part, _, first_part = normalized.partition(",")
    last_part, first_part = last_part.strip(), first_part.strip()
    if not last_part or not first_part:
        return None
    first_tokens = first_part.split()
    # "SMITH, JOHN" or "SMITH, JOHN A" -- a trailing single initial is
    # still unambiguous, and it is printed on the card, so it is kept
    # rather than dropped.
    if len(first_tokens) == 1:
        return first_part, last_part
    if len(first_tokens) == 2 and _is_initial(first_tokens[1]):
        return first_part, last_part
    return None


def _strip_suffix(tokens: list[str]) -> tuple[list[str], str]:
    """Pull a trailing generational suffix off, to be re-attached to the
    surname wherever the surname turns out to be.
    """
    bare = {s.rstrip(".") for s in SUFFIXES}
    if len(tokens) > 2 and tokens[-1].upper().rstrip(".") in bare:
        return tokens[:-1], " " + tokens[-1]
    return tokens, ""


def _split_spaced(normalized: str, order: str) -> tuple[str, str] | None:
    """Split a comma-less name according to `order`. None if ambiguous."""
    tokens, suffix = _strip_suffix(normalized.split())

    if order == "last_first":
        # 'CARTWRIGHT ANGELA' -> ('ANGELA', 'CARTWRIGHT')
        if len(tokens) == 2:
            return tokens[1], tokens[0] + suffix
        # 'MELECIO JAMIE K' -> ('JAMIE K', 'MELECIO')
        if len(tokens) == 3 and _is_initial(tokens[2]):
            return f"{tokens[1]} {tokens[2]}", tokens[0] + suffix
        return None

    # order == "first_last": 'JOHN SMITH' / 'JOHN A SMITH'
    if len(tokens) == 2:
        return tokens[0], tokens[1] + suffix
    if len(tokens) == 3 and _is_initial(tokens[1]):
        return f"{tokens[0]} {tokens[1]}", tokens[2] + suffix
    return None


def split_owner_name(raw: str, order: str = "last_first") -> tuple[str, str]:
    """Return (first_name, last_name) for the vendor CSV.

    `order` describes how the SOURCE prints comma-less names:
      "last_first"  assessor//tax-card convention (the default -- see the
                    module docstring for the sampled evidence)
      "first_last"  ordinary human ordering, for a source that uses it

    A `LAST, FIRST` name is unambiguous on its own and is parsed by the
    comma regardless of `order`. On anything ambiguous this returns
    ("", full_string) -- first name blank, whole printed name in the
    last-name field, exactly as the spec asks.
    """
    if order not in ("last_first", "first_last"):
        raise ValueError(f"order must be 'last_first' or 'first_last', got {order!r}")
    normalized = _normalize(raw)
    if not normalized:
        return "", ""
    if is_entity(normalized) or is_multiple_owners(normalized):
        return "", normalized
    split = (
        _split_last_first(normalized) if "," in normalized
        else _split_spaced(normalized, order)
    )
    if split is None:
        return "", normalized
    return split
