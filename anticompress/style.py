"""ANSI styling for the console UI.

Restrained palette: one accent (Steam sky-blue) + semantic status colors.
Honors NO_COLOR and TERM=dumb; everything degrades to plain text.
"""
from __future__ import annotations

import os
import re

_COLOR = os.environ.get("NO_COLOR") is None and os.environ.get("TERM") != "dumb"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _paint(code: int, text: str, bold: bool = False) -> str:
    if not _COLOR:
        return text
    b = "1;" if bold else ""
    return f"\x1b[{b}38;5;{code}m{text}\x1b[0m"


def accent(text: str, bold: bool = False) -> str:
    return _paint(75, text, bold)  # sky blue (Steam accent)


def green(text: str, bold: bool = False) -> str:
    return _paint(114, text, bold)


def yellow(text: str, bold: bool = False) -> str:
    return _paint(221, text, bold)


def red(text: str, bold: bool = False) -> str:
    return _paint(203, text, bold)


def muted(text: str) -> str:
    return _paint(245, text)  # light gray


def dim(text: str) -> str:
    return _paint(240, text)  # darker gray: borders, decoration


def bold(text: str) -> str:
    if not _COLOR:
        return text
    return f"\x1b[1m{text}\x1b[0m"


def strip(text: str) -> str:
    return _ANSI_RE.sub("", text)


def width(text: str) -> int:
    return len(strip(text))
