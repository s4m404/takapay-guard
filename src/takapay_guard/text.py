"""Text helpers for Bangla, Banglish and English content.

Copied unchanged from the TakaPay benchmark (s4m404/mfs-agent-security,
``mfs_env/text.py``) so the package gives the same answers. No model reads
the text: these are plain string rules.
"""

from __future__ import annotations

import re
import unicodedata

# Bangla digits ০ to ৯ map to 0 to 9
_BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")

_PHONE_RE = re.compile(r"(?:\+?88)?0?1[3-9]\d{8}")


def to_ascii_digits(text: str) -> str:
    """Convert Bangla digits to ASCII digits."""
    return text.translate(_BN_DIGITS)


def normalise_number(value: str) -> str:
    """Normalise a phone or account number.

    Handles Bangla digits, spaces, dashes and the +88 country prefix,
    so "+৮৮ ০১৮৯৯-৯৯৯৯৯৯" becomes "01899999999".
    """
    s = to_ascii_digits(str(value)).strip()
    s = re.sub(r"[\s\-()]", "", s)
    if s.startswith("+88"):
        s = s[3:]
    elif s.startswith("88") and len(s) == 13:
        s = s[2:]
    return s


def normalise_text(text: str) -> str:
    """Unicode NFC, ASCII digits, lower case, collapsed whitespace."""
    s = unicodedata.normalize("NFC", text)
    s = to_ascii_digits(s).lower()
    return re.sub(r"\s+", " ", s).strip()


def join_digit_groups(text: str) -> str:
    """Normalised text with spaces, dashes and dots between digits removed,
    so a code written "482 913" or "৪৮২-৯১৩" reads as "482913"."""
    return re.sub(r"(?<=\d)[\s\-.]+(?=\d)", "", normalise_text(text))


def extract_numbers(text: str) -> set[str]:
    """Find Bangladeshi style phone numbers in any script."""
    s = normalise_text(text)
    s = re.sub(r"(?<=\d)[\s\-](?=\d)", "", s)
    return {normalise_number(m) for m in _PHONE_RE.findall(s)}


_AMOUNT_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|november|december|"
    "জানুয়ারি|ফেব্রুয়ারি|মার্চ|এপ্রিল|মে|জুন|জুলাই|আগস্ট|সেপ্টেম্বর|অক্টোবর|নভেম্বর|ডিসেম্বর"
)
# A day of the month ("10 October", "১০ অক্টোবর", "10 tarikh", "১০ তারিখের")
# or a year after a month ("September 2026") is a date, not an amount.
_NOT_MONEY = r"(?!\d|[\d,]*\s*(?:tk|taka|টাকা|৳|bdt))"
_DATE_RE = re.compile(
    rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s*(?:{_MONTHS}|tarikh|তারিখ)"
    rf"|(?:{_MONTHS})\s*,?\s*(?:\d{{1,2}}|(?:19|20)\d\d){_NOT_MONEY}"
)


def extract_amounts(text: str) -> set[float]:
    """Numbers in a text that could be amounts (dates are left out).

    Any such number counts: the rule cannot tell a total (1800) from a share
    (600) in the same message. That is a known limit of the amount check."""
    out = set()
    norm = normalise_text(text)
    for m in _AMOUNT_RE.findall(_DATE_RE.sub(" ", norm)):
        try:
            out.add(round(float(m.replace(",", "")), 2))
        except ValueError:
            pass
    return out
