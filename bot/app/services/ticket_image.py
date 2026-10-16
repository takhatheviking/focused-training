"""Renders a ticket as a PNG: event info, holder, ticket code and the check-in QR."""

import io
import os
from dataclasses import dataclass
from functools import lru_cache

import qrcode
from PIL import Image, ImageDraw, ImageFont
from qrcode.constants import ERROR_CORRECT_M

W, H = 1000, 1450
PAD = 70
BG = "#EEF0F4"
CARD = "#FFFFFF"
INK = "#111827"
MUTED = "#6B7280"
ACCENT = "#4F46E5"
HEADER = "#111827"

REGULAR_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
]
BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


@dataclass
class TicketView:
    event_title: str
    date_line: str
    venue_line: str | None
    ticket_type: str
    holder: str
    code: str
    qr_payload: str
    order_line: str
    labels: dict[str, str]  # holder, type, code, footer


@lru_cache(maxsize=64)
def _font(bold: bool, size: int, custom: str | None) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = ([custom] if custom else []) + (BOLD_CANDIDATES if bold else REGULAR_CANDIDATES)
    for path in candidates:
        if path and os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int, max_lines: int) -> list[str]:
    words, lines, line = text.split(), [], ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width:
            line = candidate
            continue
        if line:
            lines.append(line)
        line = word
    if line:
        lines.append(line)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and draw.textlength(lines[-1] + "…", font=font) > max_width:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    return lines


def _qr_image(payload: str, size: int) -> Image.Image:
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color=INK, back_color="white").convert("RGB")
    return img.resize((size, size), Image.Resampling.NEAREST)


def render_ticket(view: TicketView, font_path: str | None = None) -> bytes:
    f = lambda bold, size: _font(bold, size, font_path)  # noqa: E731
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    card = (40, 40, W - 40, H - 40)
    d.rounded_rectangle(card, radius=36, fill=CARD)
    d.rounded_rectangle((40, 40, W - 40, 190), radius=36, fill=HEADER)
    d.rectangle((40, 150, W - 40, 190), fill=HEADER)
    d.text((PAD + 10, 82), "FOCUSED", font=f(True, 52), fill="white")
    label = "E-TICKET"
    d.text((W - PAD - 10 - d.textlength(label, font=f(True, 30)), 96), label, font=f(True, 30), fill="#A5B4FC")

    y = 240
    title_font = f(True, 54)
    for line in _wrap(d, view.event_title, title_font, W - 2 * PAD - 20, 3):
        d.text((PAD + 10, y), line, font=title_font, fill=INK)
        y += 68
    y += 12
    d.text((PAD + 10, y), view.date_line, font=f(False, 34), fill=ACCENT)
    y += 50
    if view.venue_line:
        for line in _wrap(d, view.venue_line, f(False, 32), W - 2 * PAD - 20, 2):
            d.text((PAD + 10, y), line, font=f(False, 32), fill=MUTED)
            y += 44

    # Perforation line with side notches, like a paper ticket.
    y = max(y + 30, 600)
    d.ellipse((40 - 30, y - 30, 40 + 30, y + 30), fill=BG)
    d.ellipse((W - 40 - 30, y - 30, W - 40 + 30, y + 30), fill=BG)
    for x in range(100, W - 100, 28):
        d.line((x, y, x + 14, y), fill="#D1D5DB", width=3)

    y += 40
    col2 = W // 2 + 10
    d.text((PAD + 10, y), view.labels["holder"], font=f(False, 26), fill=MUTED)
    d.text((col2, y), view.labels["type"], font=f(False, 26), fill=MUTED)
    y += 36
    holder = _wrap(d, view.holder, f(True, 34), W // 2 - PAD - 20, 1)[0]
    ttype = _wrap(d, view.ticket_type, f(True, 34), W // 2 - PAD - 20, 1)[0]
    d.text((PAD + 10, y), holder, font=f(True, 34), fill=INK)
    d.text((col2, y), ttype, font=f(True, 34), fill=INK)

    qr_size = 520
    qr_top = H - 40 - 70 - 90 - qr_size
    img.paste(_qr_image(view.qr_payload, qr_size), ((W - qr_size) // 2, qr_top))

    code_font = f(True, 64)
    d.text(((W - d.textlength(view.code, font=code_font)) / 2, qr_top + qr_size + 14), view.code, font=code_font, fill=INK)
    footer = f"{view.order_line} · {view.labels['footer']}"
    d.text(((W - d.textlength(footer, font=f(False, 24))) / 2, H - 40 - 50), footer, font=f(False, 24), fill=MUTED)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
