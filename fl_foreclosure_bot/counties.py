"""County configuration for the two realforeclose.com sites in scope.

Both counties run the same RealAuction platform (index.cfm, ?zaction=
AUCTION&Zmethod=PREVIEW&AUCTIONDATE=MM/DD/YYYY), confirmed by inspecting
real pages for both. Minor confirmed differences between them (not
assumed): Broward case numbers use a "CACE-" prefix and its cards don't
always show an "Assessed Value" line; Miami-Dade's page footer also
references tax deed sales (F.S. 197.542) that Broward's doesn't. Neither
difference changes the parsing approach in auction_page.py, which reads
whatever labels are actually present per card.
"""

COUNTIES = {
    "miami-dade": {
        "display_name": "Miami-Dade",
        "base_url": "https://www.miamidade.realforeclose.com/index.cfm",
    },
    "broward": {
        "display_name": "Broward",
        "base_url": "https://broward.realforeclose.com/index.cfm",
    },
}


def county_choices() -> list[str]:
    return list(COUNTIES.keys())
