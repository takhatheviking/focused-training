"""Live mirror of events into one Google Spreadsheet.

Layout: a tab per event with its participants (same table as the Excel report) and one
"All orders" tab. The bot rewrites a tab only when its data changed, every
SHEETS_SYNC_MINUTES, or immediately on "Sync now".

Access: a Google service account (JSON key at GOOGLE_SERVICE_ACCOUNT_FILE). New service
accounts have no Drive storage, so the bot never creates spreadsheets: an admin creates
one, shares it with the service account as Editor and gives the bot the link.
Only the Sheets scope is requested; the account sees nothing but shared spreadsheets.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta

import gspread
from gspread.utils import rowcol_to_a1
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Event, EventStatus
from app.i18n import t
from app.services import app_settings
from app.services.export import ACCENT, BAND, HEADER_ROW, Table, orders_table, participants_table, stamped_title_lines

log = logging.getLogger(__name__)
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SETTINGS_KEY = "gsheet"  # {"id": spreadsheet id, "tabs": {"orders": sheetId, "event:1": sheetId}}
SHEET_URL_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})")
BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{30,}$")
EVENT_WINDOW = timedelta(days=30)  # past events keep syncing for a month (refunds, late check-ins)


def parse_sheet_id(text: str) -> str | None:
    text = text.strip()
    if m := SHEET_URL_RE.search(text):
        return m.group(1)
    return text if BARE_ID_RE.match(text) else None


def sheet_url(sheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}"


def service_account_email(settings: Settings) -> str | None:
    path = settings.google_service_account_file
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("client_email")
    except (OSError, ValueError):
        return None


def describe_error(e: Exception) -> str:
    """Short, admin-readable text instead of gspread's raw JSON."""
    # gspread wraps a 403 into its own empty PermissionError; Google's explanation sits in the cause.
    api_error = e if isinstance(e, gspread.exceptions.APIError) else e.__cause__ or e.__context__
    if isinstance(api_error, gspread.exceptions.APIError):
        try:
            return api_error.response.json()["error"]["message"][:400]
        except (ValueError, KeyError, AttributeError):
            pass
    if isinstance(e, gspread.exceptions.SpreadsheetNotFound):
        return "spreadsheet not found or not shared with the service account"
    return f"{type(e).__name__}: {e}"[:200]


def _rgb(hex_color: str) -> dict:
    return {"red": int(hex_color[0:2], 16) / 255, "green": int(hex_color[2:4], 16) / 255, "blue": int(hex_color[4:6], 16) / 255}


def _cell(value):
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")  # ISO-like text sorts correctly in filters
    return "" if value is None else value


def render_values(table: Table, lang: str, tz) -> list[list]:
    """Rows as written to the sheet: title block, empty row, header, data."""
    width = len(table.headers)
    pad = lambda row: list(row) + [""] * (width - len(row))  # noqa: E731
    head = [pad([line]) for line in stamped_title_lines(table, lang, tz)]
    return [*head, pad([]), table.headers, *[[_cell(v) for v in row] for row in table.rows]]


def tab_title(event: Event, lang: str, tz) -> str:
    return f"#{event.id} · {event.starts_at.astimezone(tz):%d.%m} · {event.loc('title', lang)}"[:90]


def _digest(table: Table) -> str:
    payload = json.dumps([table.title_lines, table.headers, table.rows], default=str, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def format_requests(sheet_id: int, table: Table, n_values: int, created: bool) -> list[dict]:
    n_cols = len(table.headers)
    header = HEADER_ROW - 1  # 0-based
    reqs: list[dict] = [
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": 14}}},
                "fields": "userEnteredFormat.textFormat",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 3},
                "cell": {"userEnteredFormat": {"textFormat": {"fontSize": 10, "foregroundColor": _rgb("6B7280")}}},
                "fields": "userEnteredFormat.textFormat",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": header, "endRowIndex": header + 1, "startColumnIndex": 0, "endColumnIndex": n_cols},
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": _rgb(ACCENT),
                        "textFormat": {"bold": True, "foregroundColor": _rgb("FFFFFF")},
                        "verticalAlignment": "MIDDLE",
                        "wrapStrategy": "WRAP",
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment,wrapStrategy)",
            }
        },
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": HEADER_ROW, "hideGridlines": True}},
                "fields": "gridProperties(frozenRowCount,hideGridlines)",
            }
        },
        {
            "setBasicFilter": {
                "filter": {"range": {"sheetId": sheet_id, "startRowIndex": header, "endRowIndex": max(n_values, header + 2), "startColumnIndex": 0, "endColumnIndex": n_cols}}
            }
        },
    ]
    for col, width in enumerate(table.widths):
        reqs.append(
            {
                "updateDimensionProperties": {
                    "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": col, "endIndex": col + 1},
                    "properties": {"pixelSize": width * 8},
                    "fields": "pixelSize",
                }
            }
        )
    if not created:
        return reqs

    # One-time styling of a new tab: banded rows, status colours, money format.
    data = {"sheetId": sheet_id, "startRowIndex": HEADER_ROW, "startColumnIndex": 0, "endColumnIndex": n_cols}
    reqs.append(
        {"addBanding": {"bandedRange": {"range": data, "rowProperties": {"firstBandColor": _rgb("FFFFFF"), "secondBandColor": _rgb(BAND)}}}}
    )
    status_range = {"sheetId": sheet_id, "startRowIndex": HEADER_ROW, "startColumnIndex": table.status_col - 1, "endColumnIndex": table.status_col}
    for status, (fill, color) in table.styles.items():
        reqs.append(
            {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [status_range],
                        "booleanRule": {
                            "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": table.status_labels[status]}]},
                            "format": {"backgroundColor": _rgb(fill), "textFormat": {"bold": True, "foregroundColor": _rgb(color)}},
                        },
                    },
                    "index": 0,
                }
            }
        )
    for col, kind in table.kinds.items():
        if kind == "money":
            reqs.append(
                {
                    "repeatCell": {
                        "range": {"sheetId": sheet_id, "startRowIndex": HEADER_ROW, "startColumnIndex": col - 1, "endColumnIndex": col},
                        "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}},
                        "fields": "userEnteredFormat.numberFormat",
                    }
                }
            )
    return reqs


class SheetsSync:
    def __init__(self) -> None:
        self.hashes: dict[tuple[str, str], str] = {}
        self.last_run = 0.0
        self.last_sync_at: datetime | None = None
        self.last_error: str | None = None
        self.alerted_error: str | None = None
        self._client: gspread.Client | None = None
        self._client_file: str | None = None
        self._lock = asyncio.Lock()

    def enabled(self, settings: Settings) -> bool:
        return service_account_email(settings) is not None

    def due(self, settings: Settings) -> bool:
        return time.monotonic() - self.last_run >= settings.sheets_sync_minutes * 60

    async def spreadsheet_id(self, session: AsyncSession, settings: Settings) -> str | None:
        state = await app_settings.get_value(session, SETTINGS_KEY) or {}
        return state.get("id") or settings.google_sheet_id

    def _client_for(self, settings: Settings) -> gspread.Client:
        path = settings.google_service_account_file
        if self._client is None or self._client_file != path:
            self._client = gspread.service_account(filename=path, scopes=SCOPES)
            self._client_file = path
        return self._client

    def check_access(self, settings: Settings, sheet_id: str) -> str:
        """Opens the spreadsheet (blocking); returns its title or raises."""
        return self._client_for(settings).open_by_key(sheet_id).title

    async def sync(self, session: AsyncSession, settings: Settings, *, force: bool = False) -> int:
        """Rewrite tabs whose data changed. Returns how many tabs were written; raises on API errors."""
        async with self._lock:
            self.last_run = time.monotonic()
            sheet_id = await self.spreadsheet_id(session, settings)
            if not sheet_id or not self.enabled(settings):
                return 0
            lang, tz = settings.default_lang, settings.tz
            events = (
                await session.execute(
                    select(Event)
                    .where(Event.status != EventStatus.DRAFT, Event.starts_at > datetime.now(tz) - EVENT_WINDOW)
                    .order_by(Event.starts_at)
                )
            ).scalars()
            tables = [("orders", t(lang, "gs_tab_orders"), await orders_table(session, lang, tz))]
            for event in events:
                tables.append((f"event:{event.id}", tab_title(event, lang, tz), await participants_table(session, event, lang, tz)))

            state = await app_settings.get_value(session, SETTINGS_KEY) or {}
            tabs: dict[str, int] = dict(state.get("tabs", {})) if state.get("id") == sheet_id else {}
            todo = []
            for key, title, table in tables:
                digest = _digest(table) + title
                if force or key not in tabs or self.hashes.get((sheet_id, key)) != digest:
                    todo.append((key, title, table, digest))
            if not todo:
                self.last_sync_at, self.last_error = datetime.now(tz), None
                return 0

            await asyncio.to_thread(self._push, settings, sheet_id, tabs, todo, lang, tz)
            if tabs != state.get("tabs") or state.get("id") != sheet_id:
                await app_settings.set_value(session, SETTINGS_KEY, {"id": sheet_id, "tabs": tabs})
                await session.commit()
            for key, _, _, digest in todo:
                self.hashes[(sheet_id, key)] = digest
            self.last_sync_at, self.last_error = datetime.now(tz), None
            return len(todo)

    def _push(self, settings: Settings, sheet_id: str, tabs: dict[str, int], todo: list, lang: str, tz) -> None:
        sh = self._client_for(settings).open_by_key(sheet_id)
        existing = {ws.id: ws for ws in sh.worksheets()}
        by_title = {ws.title: ws for ws in existing.values()}
        for key, title, table, _ in todo:
            values = render_values(table, lang, tz)
            ws = existing.get(tabs.get(key)) or by_title.get(title)  # adopt a tab whose id mapping was lost
            created = ws is None
            if created:
                ws = sh.add_worksheet(title=title, rows=len(values) + 100, cols=len(table.headers) + 2)
                existing[ws.id] = ws
            else:
                if ws.title != title and title not in by_title:
                    ws.update_title(title)
                if ws.row_count < len(values) + 10 or ws.col_count < len(table.headers):
                    ws.resize(rows=max(ws.row_count, len(values) + 100), cols=max(ws.col_count, len(table.headers) + 2))
                last = rowcol_to_a1(ws.row_count, ws.col_count)
                ws.batch_clear([f"A1:{last}"])
            tabs[key] = ws.id
            by_title[ws.title] = ws
            ws.update(values, "A1", raw=True)  # RAW: names starting with "=" stay text
            sh.batch_update({"requests": format_requests(ws.id, table, len(values), created)})


sheets = SheetsSync()
