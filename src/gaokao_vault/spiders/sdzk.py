"""Parsers for Shandong Education Admission and Examination Authority (sdzk.cn) attachments.

The authority publishes admission (投档情况表) and score segment (一分一段表) tables as
Excel attachments. Layouts drift between years (extra blank first column, two-row headers,
optional 层次 / 选考科目要求 / 最低分 columns), and the 2020 segment table is an Excel HTML
export saved as ``.xls``, so columns are located by header keywords rather than position.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

SDZK_BASE_URL = "https://www.sdzk.cn"
SDZK_DATA_SOURCE = "sdzk.cn"
SDZK_ADMISSION_LIST_URL = f"{SDZK_BASE_URL}/NewsList.aspx?BCID=1198&CID=47"
SDZK_SEGMENT_LIST_URL = f"{SDZK_BASE_URL}/NewsList.aspx?BCID=1198&CID=49"

_ROUND_NUMERALS = {"1": 1, "2": 2, "3": 3, "一": 1, "二": 2, "三": 3}
_ADMISSION_TITLE_PATTERN = re.compile(r"(\d{4})年普通类常规批第([123一二三])次志愿投档情况表")
_SEGMENT_TITLE_PATTERN = re.compile(r"^(\d{4})年夏季高考文化(?:总)?成绩一分一段表$")
_SCHOOL_CELL_PATTERN = re.compile(r"^([A-Z]\d{3})\s*(.+)$")
_MAJOR_CELL_PATTERN = re.compile(r"^([0-9A-Z]{2})\s*(.+)$")
_PAREN_GROUP_PATTERN = re.compile(r"\(([^()]*)\)")
_LEADING_INT_PATTERN = re.compile(r"^\s*(\d+)")
_FULLWIDTH_LEFT_PAREN = chr(0xFF08)
_FULLWIDTH_RIGHT_PAREN = chr(0xFF09)
_NOTE_SEPARATOR = chr(0xFF1B)

ADMISSION_BATCH = "常规批"


@dataclass(frozen=True)
class SdzkAdmissionTitle:
    year: int
    admission_round: int


@dataclass(frozen=True)
class SdzkAdmissionRecord:
    education_level: str | None
    school_code_raw: str
    school_name_raw: str
    major_code_raw: str
    major_name_raw: str
    major_name: str
    major_note: str | None
    selection_requirement: str | None
    min_rank: int | None
    min_rank_raw: str | None
    min_score: int | None
    plan_count: int | None
    toudang_plan_count: int | None
    admitted_count: int | None


@dataclass(frozen=True)
class SdzkSegmentRecord:
    score: int
    segment_count: int
    cumulative_count: int


def parse_admission_title(title: str) -> SdzkAdmissionTitle | None:
    match = _ADMISSION_TITLE_PATTERN.search(title)
    if match is None:
        return None
    return SdzkAdmissionTitle(year=int(match.group(1)), admission_round=_ROUND_NUMERALS[match.group(2)])


def parse_segment_title(title: str) -> int | None:
    match = _SEGMENT_TITLE_PATTERN.match(title.strip())
    return int(match.group(1)) if match else None


def read_table_rows(body: bytes) -> list[list[str]]:
    """Return the first sheet as a grid of stripped strings (xls, or an Excel HTML export)."""
    if body.lstrip()[:1] == b"<":
        return _read_html_table_rows(body)

    import xlrd

    sheet = xlrd.open_workbook(file_contents=body).sheet_by_index(0)
    return [[_cell_to_text(value) for value in sheet.row_values(row)] for row in range(sheet.nrows)]


def parse_admission_rows(rows: list[list[str]]) -> list[SdzkAdmissionRecord]:
    header_start = next((i for i, row in enumerate(rows) if any("院校" in cell for cell in row)), None)
    if header_start is None:
        return []

    columns = _admission_columns(rows, header_start)
    if "school" not in columns or "major" not in columns:
        return []

    records = []
    for row in rows[header_start + 1 :]:
        record = _admission_record(row, columns)
        if record is not None:
            records.append(record)
    return records


def parse_segment_rows(rows: list[list[str]]) -> list[SdzkSegmentRecord]:
    """Extract the 全体 (all candidates) columns of a summer gaokao score segment table."""
    header_row = next((i for i, row in enumerate(rows) if row and row[0] == "分数段"), None)
    if header_row is None or header_row + 1 >= len(rows):
        return []

    total_col = next((i for i, cell in enumerate(rows[header_row]) if cell == "全体"), None)
    if total_col is None:
        return []
    sub_header = rows[header_row + 1]
    segment_col = _find_column(sub_header, "本段", start=total_col)
    cumulative_col = _find_column(sub_header, "累计", start=total_col)
    if segment_col is None or cumulative_col is None:
        return []

    records = []
    for row in rows[header_row + 2 :]:
        score = _parse_int(_cell(row, 0))
        segment_count = _parse_int(_cell(row, segment_col))
        cumulative_count = _parse_int(_cell(row, cumulative_col))
        if score is None or segment_count is None or cumulative_count is None:
            continue
        records.append(SdzkSegmentRecord(score=score, segment_count=segment_count, cumulative_count=cumulative_count))
    return records


def split_major_name(name_raw: str) -> tuple[str, str | None]:
    """Split "土木工程(高校专项计划)(要求...)" into the major name and its parenthesised notes.

    Full-width brackets are normalised first; multiple notes are joined with a full-width semicolon.
    """
    normalized = name_raw.replace(_FULLWIDTH_LEFT_PAREN, "(").replace(_FULLWIDTH_RIGHT_PAREN, ")").strip()
    cut = normalized.find("(")
    if cut <= 0:
        return normalized, None
    name, remainder = normalized[:cut].strip(), normalized[cut:]
    notes = [note.strip() for note in _PAREN_GROUP_PATTERN.findall(remainder) if note.strip()]
    if not notes:
        notes = [remainder.strip("() ")]
    return name, _NOTE_SEPARATOR.join(notes) or None


def _admission_columns(rows: list[list[str]], header_start: int) -> dict[str, int]:
    width = max(len(row) for row in rows)
    data_start = next(
        (i for i in range(header_start + 1, len(rows)) if any(_SCHOOL_CELL_PATTERN.match(cell) for cell in rows[i])),
        len(rows),
    )
    headers = ["".join(_cell(rows[r], col) for r in range(header_start, data_start)) for col in range(width)]

    columns: dict[str, int] = {}
    for col, header in enumerate(headers):
        key = _admission_header_key(header)
        if key is not None and key not in columns:
            columns[key] = col
    return columns


def _admission_header_key(header: str) -> str | None:
    rules = (
        ("院校", "school"),
        ("专业", "major"),
        ("层次", "level"),
        ("选考", "selection"),
        ("位次", "min_rank"),
        ("最低分", "min_score"),
        ("投档计划数", "toudang_plan"),
        ("计划数", "plan"),
        ("投出数", "admitted"),
    )
    for keyword, key in rules:
        if keyword in header:
            return key
    return None


def _admission_record(row: list[str], columns: dict[str, int]) -> SdzkAdmissionRecord | None:
    school_match = _SCHOOL_CELL_PATTERN.match(_cell(row, columns["school"]))
    major_match = _MAJOR_CELL_PATTERN.match(_cell(row, columns["major"]))
    if school_match is None or major_match is None:
        return None

    major_name_raw = major_match.group(2).strip()
    major_name, major_note = split_major_name(major_name_raw)
    rank_text = _optional_cell(row, columns.get("min_rank"))
    min_rank = _parse_int(rank_text) if rank_text and rank_text.isdigit() else None
    return SdzkAdmissionRecord(
        education_level=_optional_cell(row, columns.get("level")),
        school_code_raw=school_match.group(1),
        school_name_raw=school_match.group(2).strip(),
        major_code_raw=major_match.group(1),
        major_name_raw=major_name_raw,
        major_name=major_name,
        major_note=major_note,
        selection_requirement=_optional_cell(row, columns.get("selection")),
        min_rank=min_rank,
        min_rank_raw=rank_text if min_rank is None else None,
        min_score=_parse_int(_optional_cell(row, columns.get("min_score"))),
        plan_count=_parse_int(_optional_cell(row, columns.get("plan"))),
        toudang_plan_count=_parse_int(_optional_cell(row, columns.get("toudang_plan"))),
        admitted_count=_parse_int(_optional_cell(row, columns.get("admitted"))),
    )


def _read_html_table_rows(body: bytes) -> list[list[str]]:
    """Expand an Excel HTML export's table (with rowspan/colspan) into a grid."""
    from scrapling.parser import Adaptor

    page = Adaptor(content=body.decode("utf-8", errors="ignore"))
    grid: list[list[str]] = []
    carried: dict[tuple[int, int], str] = {}
    for row_index, tr in enumerate(page.css("table tr")):
        row: list[str] = []
        col = 0
        for td in tr.css("td, th"):
            while (row_index, col) in carried:
                row.append(carried.pop((row_index, col)))
                col += 1
            text = "".join(td.css("::text").getall()).strip()
            rowspan = _parse_int(td.attrib.get("rowspan")) or 1
            colspan = _parse_int(td.attrib.get("colspan")) or 1
            for offset in range(colspan):
                row.append(text if offset == 0 else "")
                for extra_row in range(1, rowspan):
                    carried[(row_index + extra_row, col + offset)] = text if offset == 0 else ""
            col += colspan
        while (row_index, col) in carried:
            row.append(carried.pop((row_index, col)))
            col += 1
        grid.append(row)
    return grid


def _cell_to_text(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _cell(row: list[str], col: int) -> str:
    return row[col].strip() if 0 <= col < len(row) else ""


def _optional_cell(row: list[str], col: int | None) -> str | None:
    if col is None:
        return None
    return _cell(row, col) or None


def _find_column(row: list[str], keyword: str, *, start: int) -> int | None:
    return next((i for i in range(start, min(start + 2, len(row))) if keyword in row[i]), None)


def _parse_int(value: str | None) -> int | None:
    if not value:
        return None
    match = _LEADING_INT_PATTERN.match(value)
    return int(match.group(1)) if match else None
