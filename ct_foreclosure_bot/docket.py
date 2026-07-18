"""Fetches and parses a case's docket page.

LoadDocket.aspx?DocketNo=X redirects to
CaseDetail/PublicCaseDetail.aspx?DocketNo=<no-dashes>. The motions/
documents table there (id ..._gvDocuments, located by its "Entry No" header
in practice since the literal id can vary) lists one row per docket entry:
Entry No | File Date | Filed By | Description | Arguable. Entries with a
document on file have their Description text as a link to
DocumentInquiry.aspx?DocumentNo=N; entries with "(NO DOCUMENT)" or similar
administrative-only entries do not.
"""

import re
from urllib.parse import urljoin

from playwright.async_api import BrowserContext

from .browser import BASE_URL
from .models import DocketEntry, DocketInfo
from .throttle import Throttle

LOAD_DOCKET_URL = f"{BASE_URL}/LoadDocket.aspx"


async def fetch_docket_html(context: BrowserContext, throttle: Throttle, docket_no: str) -> tuple[str, str]:
    """Fetch the case detail page HTML. Returns (html, final_url)."""
    await throttle.wait()
    resp = await context.request.get(f"{LOAD_DOCKET_URL}?DocketNo={docket_no}", timeout=30000)
    html = await resp.text()
    return html, resp.url


def parse_case_caption(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    caption = soup.find(id=re.compile(r"lblCaseCaption$"))
    if caption:
        return caption.get_text(" ", strip=True)
    title = soup.find("title")
    return title.get_text(strip=True) if title else ""


def parse_docket_entries(html: str, page_url: str) -> list[DocketEntry]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    target_table = None
    for table in soup.find_all("table"):
        if "Entry No" in table.get_text()[:200]:
            target_table = table
            break
    if target_table is None:
        return []

    entries = []
    for tr in target_table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) != 5:
            continue  # skip header/pager rows
        entry_no, file_date, filed_by, desc_cell, arguable = cells
        entry_no_text = entry_no.get_text(strip=True)
        file_date_text = file_date.get_text(strip=True)
        if not file_date_text:
            continue  # blank filler row
        link = desc_cell.find("a")
        doc_url = None
        if link is not None:
            href = link.get("href", "")
            if "DocumentInquiry" in href:
                doc_url = urljoin(page_url, href)
        entries.append(
            DocketEntry(
                entry_no=entry_no_text,
                file_date=file_date_text,
                filed_by=filed_by.get_text(strip=True),
                description=desc_cell.get_text(" ", strip=True),
                arguable=arguable.get_text(strip=True),
                document_url=doc_url,
            )
        )
    return entries


async def fetch_docket(context: BrowserContext, throttle: Throttle, docket_no: str) -> DocketInfo:
    html, final_url = await fetch_docket_html(context, throttle, docket_no)
    return DocketInfo(
        docket_no=docket_no,
        case_caption=parse_case_caption(html),
        case_detail_url=final_url,
        entries=parse_docket_entries(html, final_url),
    )
