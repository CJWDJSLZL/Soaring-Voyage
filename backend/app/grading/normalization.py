"""Deterministic answer normalization for primary-school mathematics."""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from fractions import Fraction

_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_SMALL_UNITS = {"十": 10, "百": 100, "千": 1000}
_BIG_UNITS = {"万": 10_000, "亿": 100_000_000}
_UNIT_WORDS = (
    "千克",
    "公斤",
    "厘米",
    "毫米",
    "公里",
    "平方米",
    "立方米",
    "小时",
    "分钟",
    "元",
    "角",
    "分",
    "米",
    "克",
    "吨",
    "秒",
    "个",
    "只",
    "本",
    "支",
    "张",
    "岁",
)


def chinese_integer(text: str) -> int | None:
    """Parse a non-negative Chinese integer without third-party heuristics."""
    if not text or any(ch not in _DIGITS and ch not in _SMALL_UNITS and ch not in _BIG_UNITS for ch in text):
        return None
    if all(ch in _DIGITS for ch in text):
        return int("".join(str(_DIGITS[ch]) for ch in text))
    total = section = number = 0
    for ch in text:
        if ch in _DIGITS:
            number = _DIGITS[ch]
        elif ch in _SMALL_UNITS:
            unit = _SMALL_UNITS[ch]
            section += (number or 1) * unit
            number = 0
        else:
            section += number
            total += section * _BIG_UNITS[ch]
            section = number = 0
    return total + section + number


def _chinese_number(text: str) -> str | None:
    sign = ""
    if text.startswith(("负", "負")):
        sign, text = "-", text[1:]
    if "点" in text:
        integer, decimal = text.split("点", 1)
        whole = chinese_integer(integer) if integer else 0
        if whole is None or not decimal or any(ch not in _DIGITS for ch in decimal):
            return None
        return sign + str(whole) + "." + "".join(str(_DIGITS[ch]) for ch in decimal)
    value = chinese_integer(text)
    return None if value is None else sign + str(value)


def _canonical_numeric(text: str) -> str:
    try:
        if re.fullmatch(r"[+-]?\d+/[+-]?\d+", text):
            numerator, denominator = text.split("/", 1)
            if int(denominator) == 0:
                return text
            return str(Fraction(int(numerator), int(denominator)))
        if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", text):
            return str(Fraction(Decimal(text)))
    except (InvalidOperation, ValueError, ZeroDivisionError):
        pass
    return text


def normalize_answer(answer: object, question_type: str | None = None) -> str:
    """Return a comparison-safe representation of a student's answer."""
    text = unicodedata.normalize("NFKC", "" if answer is None else str(answer)).strip()
    text = text.replace("，", ",").replace("：", ":").replace("／", "/")
    if question_type == "choice":
        match = re.search(r"(?i)(?:选项|答案)?\s*[\(（\[]?\s*([A-D])\s*[\)）\]]?", text)
        return match.group(1).upper() if match else ""

    text = re.sub(r"^(?:答案|答)\s*[:：]?\s*", "", text)
    text = re.sub(r"\s+", "", text)
    # Remove common trailing measurement/counting units, longest first.
    changed = True
    while changed:
        changed = False
        for unit in _UNIT_WORDS:
            prefix = text[: -len(unit)] if text.endswith(unit) else ""
            numeric_prefix = bool(prefix) and (
                re.fullmatch(r"[0-9.+\-*/()^]+", prefix) is not None or _chinese_number(prefix) is not None
            )
            if prefix and numeric_prefix:
                text = prefix
                changed = True
                break

    fraction = re.fullmatch(
        r"([负負零〇一二两三四五六七八九十百千万亿]+)分之([负負零〇一二两三四五六七八九十百千万亿]+)", text
    )
    if fraction:
        denominator = _chinese_number(fraction.group(1))
        numerator = _chinese_number(fraction.group(2))
        if denominator is not None and numerator is not None:
            return _canonical_numeric(f"{numerator}/{denominator}")

    chinese = _chinese_number(text)
    if chinese is not None:
        text = chinese
    elif question_type == "fill_blank" and any(ch.isalpha() for ch in text):
        # Preserve textual fill-ins while dropping surrounding punctuation,
        # emoji, and other symbols just as numeric normalization does.
        return "".join(ch for ch in text.casefold() if ch.isalnum())
    text = text.replace("×", "*").replace("÷", "/").replace("−", "-")
    # Keep only the small arithmetic alphabet; this also removes emoji/punctuation.
    text = "".join(ch for ch in text if ch.isdigit() or ch in ".+-*/()^")
    return _canonical_numeric(text)
