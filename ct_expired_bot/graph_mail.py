"""Step 1 over Microsoft Graph, for mailboxes on Exchange Online.

Why this module exists: Microsoft disabled basic-auth IMAP/POP on
Exchange Online, so `alerts.load_from_imap` cannot authenticate against
an M365 mailbox no matter what password you give it. Graph with OAuth2
is the supported path, and it is the one to use for a
`*.mail.protection.outlook.com` domain.

AUTH FLOW: device code, against a PUBLIC client app -- so there is no
client secret anywhere in this repo, in your environment, or on disk.
You sign in once in a browser, and a refresh token is cached locally so
scheduled runs work unattended.

    ON THE TOKEN CACHE: it holds a refresh token, which is a real
    credential -- it can read your mail until revoked. The file is
    written 0600 and defaults to ~/.ct_expired_bot/graph-token.json.
    This is a deliberate exception to the "no stored credentials" rule,
    which exists for the MLS session; unattended email access is not
    possible without it. Revoke any time from Entra ID, or just delete
    the file to force a fresh sign-in.

ONE-TIME APP REGISTRATION (you are your own tenant admin, so you can do
all of this yourself):

  1. Entra ID -> App registrations -> New registration
     Name: anything. Account types: "this organizational directory only".
     No redirect URI needed.
  2. On the new app: Authentication -> "Allow public client flows" -> Yes
  3. API permissions -> Add -> Microsoft Graph -> Delegated -> Mail.Read
     -> Add, then "Grant admin consent"
  4. Copy the Application (client) ID and the Directory (tenant) ID

Then:
    python -m ct_expired_bot --graph-login \
        --graph-client-id <app id> --graph-tenant <tenant id>

Mail.Read is read-only and delegated -- it can read your mailbox as you,
and cannot send, delete, or reach anyone else's mail.
"""

import json
import logging
import os
import stat
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .alerts import parse_alert_html
from .models import AlertListing

log = logging.getLogger(__name__)

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
DEFAULT_SCOPES = ["Mail.Read"]
DEFAULT_TOKEN_CACHE = Path.home() / ".ct_expired_bot" / "graph-token.json"
PAGE_SIZE = 50
MAX_PAGES = 20  # a safety stop; 1000 messages is far past any alert volume


class GraphAuthError(RuntimeError):
    """Raised when no usable token can be obtained."""


def _require_msal():
    try:
        import msal  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on install
        raise GraphAuthError(
            "The Microsoft Graph path needs the 'msal' package: pip install msal"
        ) from exc
    return __import__("msal")


def _load_cache(msal, cache_path: Path):
    cache = msal.SerializableTokenCache()
    if cache_path.exists():
        try:
            cache.deserialize(cache_path.read_text())
        except Exception as exc:
            log.warning("could not read token cache %s: %s", cache_path, exc)
    return cache


def _save_cache(cache, cache_path: Path) -> None:
    if not cache.has_state_changed:
        return
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(cache.serialize())
    try:
        os.chmod(cache_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:  # pragma: no cover - platform dependent
        pass


def _build_app(msal, client_id: str, tenant: str, cache):
    return msal.PublicClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant}",
        token_cache=cache,
    )


def acquire_token(
    client_id: str,
    tenant: str,
    cache_path: Path | None = None,
    interactive: bool = False,
    scopes: list[str] | None = None,
) -> str:
    """Return a Graph access token.

    Silent (cached refresh token) unless `interactive`, which runs the
    device-code flow and prints the code for you to enter in a browser.
    """
    msal = _require_msal()
    scopes = scopes or DEFAULT_SCOPES
    cache_path = Path(cache_path) if cache_path else DEFAULT_TOKEN_CACHE
    cache = _load_cache(msal, cache_path)
    app = _build_app(msal, client_id, tenant, cache)

    result = None
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(scopes, account=accounts[0])

    if not result and interactive:
        flow = app.initiate_device_flow(scopes=scopes)
        if "user_code" not in flow:
            raise GraphAuthError(f"Could not start device flow: {flow.get('error_description', flow)}")
        print("\n" + flow["message"] + "\n", flush=True)
        result = app.acquire_token_by_device_flow(flow)

    _save_cache(cache, cache_path)

    if not result or "access_token" not in result:
        detail = (result or {}).get("error_description", "no cached token")
        raise GraphAuthError(
            f"Could not get a Graph token ({detail}). Run with --graph-login to sign in."
        )
    return result["access_token"]


def _graph_get(url: str, token: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            # Ask for HTML bodies; the alert layout is a table.
            "Prefer": 'outlook.body-content-type="html"',
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:400]
        raise GraphAuthError(f"Graph request failed ({exc.code}): {body}") from exc


def find_folder_id(token: str, display_name: str) -> str | None:
    """Resolve a mail folder by display name, searching nested folders.

    Nested search matters: the alert folder observed in this mailbox
    ("CT-Expired Listings") is a child of Inbox, not a top-level folder,
    so a flat /me/mailFolders lookup would miss it.
    """
    if not display_name:
        return None
    queue = [f"{GRAPH_ROOT}/me/mailFolders?$top=100&$select=id,displayName"]
    seen = 0
    while queue and seen < 200:
        payload = _graph_get(queue.pop(0), token)
        for folder in payload.get("value", []):
            seen += 1
            if (folder.get("displayName") or "").strip().lower() == display_name.strip().lower():
                return folder.get("id")
            queue.append(
                f"{GRAPH_ROOT}/me/mailFolders/{folder['id']}/childFolders"
                "?$top=100&$select=id,displayName"
            )
        if payload.get("@odata.nextLink"):
            queue.append(payload["@odata.nextLink"])
    return None


def _messages_url(
    since: datetime | None, sender_filter: str | None, folder_id: str | None = None
) -> str:
    clauses = []
    if since is not None:
        stamp = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        clauses.append(f"receivedDateTime ge {stamp}")
    if sender_filter:
        escaped = sender_filter.replace("'", "''")
        clauses.append(f"from/emailAddress/address eq '{escaped}'")

    params = {
        "$select": "id,subject,receivedDateTime,from,body",
        "$top": str(PAGE_SIZE),
        "$orderby": "receivedDateTime desc",
    }
    if clauses:
        params["$filter"] = " and ".join(clauses)
    base = (
        f"{GRAPH_ROOT}/me/mailFolders/{folder_id}/messages"
        if folder_id else f"{GRAPH_ROOT}/me/messages"
    )
    return base + "?" + urllib.parse.urlencode(params)


def message_to_listings(message: dict, subject_filter: str = "") -> list[AlertListing]:
    """Convert one Graph message payload into alert rows.

    Subject filtering happens here rather than in the query because Graph
    does not support contains() on subject in $filter -- doing it wrong
    server-side silently returns everything.
    """
    subject = message.get("subject") or ""
    if subject_filter and subject_filter.lower() not in subject.lower():
        return []
    body = (message.get("body") or {}).get("content") or ""
    if not body:
        return []
    sender = ((message.get("from") or {}).get("emailAddress") or {}).get("address", "")
    return parse_alert_html(
        body,
        source=f"graph:{message.get('id', '')[:24]}|{sender}",
        received_at=message.get("receivedDateTime") or "",
    )


def load_from_graph(
    client_id: str,
    tenant: str,
    since: datetime | None = None,
    sender_filter: str | None = None,
    subject_filter: str = "",
    folder: str = "",
    cache_path: Path | None = None,
) -> list[AlertListing]:
    """Fetch and parse alert messages from an M365 mailbox.

    `subject_filter` defaults to empty on purpose. The alerts in this
    mailbox arrive under the agent's own name with subjects like
    "Updated Matches for Matt Expired - 400K less" -- nothing in the
    sender or subject says "SmartMLS", so the old default of "SmartMLS"
    matched zero messages. Scope the run with `folder` instead, and use
    `subject_filter` only as a narrowing extra.
    """
    token = acquire_token(client_id, tenant, cache_path=cache_path)
    folder_id = find_folder_id(token, folder) if folder else None
    if folder and not folder_id:
        raise GraphAuthError(
            f"Mail folder {folder!r} not found. Check the name in Outlook "
            "(it is matched case-insensitively, including nested folders)."
        )
    url = _messages_url(since, sender_filter, folder_id)

    collected: list[AlertListing] = []
    for _ in range(MAX_PAGES):
        payload = _graph_get(url, token)
        for message in payload.get("value", []):
            collected.extend(message_to_listings(message, subject_filter=subject_filter))
        url = payload.get("@odata.nextLink")
        if not url:
            break
    return collected


def describe_mailbox(client_id: str, tenant: str, cache_path: Path | None = None) -> str:
    """Who are we signed in as -- used by --graph-login to confirm."""
    token = acquire_token(client_id, tenant, cache_path=cache_path)
    me = _graph_get(f"{GRAPH_ROOT}/me?$select=displayName,mail,userPrincipalName", token)
    return me.get("mail") or me.get("userPrincipalName") or me.get("displayName") or "(unknown)"
