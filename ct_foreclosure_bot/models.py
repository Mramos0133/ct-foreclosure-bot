"""Shared data structures passed between pipeline stages."""

from dataclasses import dataclass, field


@dataclass
class CaseListing:
    """One row from a Property Address Search results grid."""
    city_town: str
    street_address: str
    zip_code: str
    case_type: str
    case_name: str
    docket_no: str
    property_type: str
    disposition: str
    search_town: str  # the town query that produced this row


@dataclass
class DocketEntry:
    entry_no: str
    file_date: str
    filed_by: str
    description: str
    arguable: str
    document_url: str | None = None


@dataclass
class DocketInfo:
    docket_no: str
    case_caption: str
    case_detail_url: str
    entries: list[DocketEntry] = field(default_factory=list)


@dataclass
class WorksheetFields:
    document_no: str
    document_url: str
    appraised_value: float | None = None  # worksheet line 1: "Fair market value of property being foreclosed"
    updated_debt: float | None = None
    total_debt_plus_prior_encumbrances: float | None = None
    encumbrances_subsequent_to_lien: float | None = None
    attorney_fees_raw: str | None = None
    appraisal_fee: float | None = None  # the fee paid *for* the appraisal (a small service cost) -- distinct from appraised_value above
    title_search_fee: float | None = None
    ocr_validated: bool = False
    ocr_warning: str | None = None
    raw_lines: dict = field(default_factory=dict)


@dataclass
class CaseResult:
    town: str
    docket_no: str
    street_address: str
    zip_code: str
    case_caption: str
    motion_types_found: list[str]
    total_debt: float | None
    appraised_value: float | None
    encumbrances_subsequent_itemized: str
    encumbrances_subsequent_to_lien: float | None
    attorney_fees: str | None
    default_failure_to_appear: bool
    bankruptcy_stay_reopened: bool
    bankruptcy_supporting_text: str
    case_detail_url: str
    worksheet_doc_url: str | None
    # Lead-ranking fields (see lead_ranking.py for how these are derived)
    lead_bucket: str = "UNCLASSIFIED"  # "HOT" | "WARM" | "COLD" | "POTENTIAL_SHORT_SALE" | "UNCLASSIFIED"
    judgment_granted: bool = False
    non_appearing: bool = False  # proxy: default_failure_to_appear
    on_auction_site: bool | None = None  # None = not applicable (e.g. strict foreclosure has no auction)
    key_date: str | None = None  # Law Day or Sale Date, ISO format, from the Order document
    key_date_label: str | None = None  # "Law Day" | "Sale Date" | None
    days_to_key_date: int | None = None
    bankruptcy_chapter: str | None = None  # "7" | "13" | etc., WARM cases only
    continuance_count: int = 0
    warm_cold_subflag: bool = False  # COLD sub-segment: 3+ continuances, no bankruptcy, non-appearing
    # Contact-tracing fields (see case_analysis.py: Return of Service,
    # Appearance, and Motion to Substitute Party detection). Owner Name is
    # parsed from the case caption (reliable); address/phone/decision-maker
    # name are intentionally left blank for manual entry after following
    # the source-document link(s) -- these documents are freeform legal
    # filings, not a fixed-template form, so auto-OCR extraction was not
    # trusted enough to populate a live outreach list unattended.
    owner_name: str = ""
    owner_phone: str = ""
    owner_address: str = ""
    owner_address_source_urls: str = ""  # Return of Service / Appearance doc URL(s), "; "-joined
    new_decision_maker_name: str = ""
    new_decision_maker_phone: str = ""
    new_decision_maker_source_url: str = ""  # Motion to Substitute Party doc URL, if found
    focus_contact: str = "Owner"  # "Owner" | "New Decision Maker"
