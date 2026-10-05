"""Pantheon's look in an email: deep navy, warm ink, gold (web/app/globals.css).

Inline styles and tables only: mail clients drop style sheets.
"""

BG = "#070b18"
SURFACE = "#10172c"
PANEL = "#18213b"
LINE = "#2a3456"
INK = "#ece6d6"
INK2 = "#aeb4c6"
GOLD = "#e8bc62"

FONT = "-apple-system,Segoe UI,Helvetica,Arial,sans-serif"
SERIF = "Georgia,'Times New Roman',serif"

CARD = (
    f"margin:24px 0 0;padding:18px 18px 16px;border-radius:14px;"
    f"background:{PANEL};border:1px solid {LINE}"
)
BUTTON = (
    f"display:inline-block;padding:11px 20px;border:0;border-radius:999px;"
    f"background:{GOLD};color:{BG};font:600 15px {FONT};cursor:pointer"
)
