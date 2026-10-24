from __future__ import annotations

from gaokao_vault.spiders.sdzk import (
    SdzkAdmissionTitle,
    parse_admission_rows,
    parse_admission_title,
    parse_segment_rows,
    parse_segment_title,
    read_table_rows,
    split_major_name,
)

_FW_LEFT, _FW_RIGHT, _FW_SEMICOLON = chr(0xFF08), chr(0xFF09), chr(0xFF1B)

# Layouts mirror real sdzk.cn attachments (values trimmed).
_ADMISSION_2020 = [
    ["", "山东省2020年普通类常规批第1次志愿投档情况表", "", "", "", "", "", ""],
    ["", "专业", "院校", "普通类", "", "", "", ""],
    ["", "", "", "计划数", "投档计划数", "投出数", "最低分", "最低位次"],
    ["", "02中国语言文学类", "A001北京大学", "2", "2", "2", "688", "194"],
    ["", "16文科试验班类(通用类)", "A001北京大学", "22", "22", "22", "679", "162"],
]

_ADMISSION_2026 = [
    ["山东省2026年普通类常规批第1次志愿投档情况表", "", "", ""],
    ["专业代号及名称", "院校代号及名称", "选考科目要求", "最低位次"],
    ["17 文科试验班类(不限选考科目类专业)", "A001北京大学", "不限", "216"],
    ["04 计算机类(高校专项计划)", "A003清华大学", "物理、化学", "前50名"],
    ["G3 统计学(中外高水平大学学生交流计划)", "Y031烟台大学", "物理、化学", "297932"],
]

_ADMISSION_ROUND_2 = [
    ["", "山东省2025年普通类常规批第2次志愿投档情况表", "", "", "", ""],
    ["", "层次", "专业代号及名称", "院校代号及名称", "投档计划数", "最低位次"],
    ["", "本科", "52 城乡规划", "A019中国农业大学", "1", "41446"],
    ["", "", "", "", "", ""],
]

_SEGMENT_XLS = [
    ["2026年夏季高考文化成绩一分一段表", "", "", "", ""],
    ["分数段", "全体", "", "选考物理", ""],
    ["", "本段人数", "累计人数", "本段人数", "累计人数"],
    ["697", "7", "56", "7", "56"],
    ["696", "3", "59", "3", "59"],
    ["150", "54", "708817", "13", "343381"],
]

# 2020's segment table is an Excel HTML export with merged header cells and 全体 last.
_SEGMENT_HTML_2020 = """
<html><body><table>
  <tr><td colspan="5">2020夏季高考文化总成绩一分一段表</td></tr>
  <tr><td rowspan="2">分数段</td><td colspan="2">选考物理</td><td colspan="2">全体</td></tr>
  <tr><td>本段人数</td><td>累计人数</td><td>本段人数</td><td>累计人数</td></tr>
  <tr><td>696</td><td>9</td><td>56</td><td>9</td><td>56</td></tr>
  <tr><td>695</td><td>4</td><td>60</td><td>5</td><td>61</td></tr>
  <tr><td></td><td></td></tr>
</table></body></html>
""".encode()


def test_parse_admission_title_handles_digits_and_chinese_numerals():
    assert parse_admission_title("山东省2026年普通类常规批第1次志愿投档情况表") == SdzkAdmissionTitle(2026, 1)
    assert parse_admission_title("山东省2022年普通类常规批第三次志愿投档情况表") == SdzkAdmissionTitle(2022, 3)
    assert parse_admission_title("山东省2026年体育类常规批第3次志愿投档情况表") is None


def test_parse_segment_title_only_matches_summer_culture_table():
    assert parse_segment_title("2026年夏季高考文化成绩一分一段表") == 2026
    assert parse_segment_title("2021年夏季高考文化总成绩一分一段表") == 2021
    assert parse_segment_title("2026年春季高考成绩一分一段表") is None
    assert parse_segment_title("2020年夏季高考和普通高中学业水平等级模拟考试文化成绩一分一段表") is None


def test_parse_admission_rows_reads_two_row_header_with_scores():
    records = parse_admission_rows(_ADMISSION_2020)

    assert len(records) == 2
    first = records[0]
    assert (first.school_code_raw, first.school_name_raw) == ("A001", "北京大学")
    assert (first.major_code_raw, first.major_name) == ("02", "中国语言文学类")
    assert (first.plan_count, first.toudang_plan_count, first.admitted_count) == (2, 2, 2)
    assert (first.min_score, first.min_rank) == (688, 194)
    assert records[1].major_note == "通用类"


def test_parse_admission_rows_keeps_non_numeric_rank_as_raw_text():
    records = parse_admission_rows(_ADMISSION_2026)

    assert [record.major_code_raw for record in records] == ["17", "04", "G3"]
    assert records[0].selection_requirement == "不限"
    assert records[0].min_rank == 216
    assert records[1].min_rank is None
    assert records[1].min_rank_raw == "前50名"
    assert records[2].school_code_raw == "Y031"
    assert records[2].min_score is None


def test_parse_admission_rows_reads_education_level_and_skips_blank_rows():
    records = parse_admission_rows(_ADMISSION_ROUND_2)

    assert len(records) == 1
    assert records[0].education_level == "本科"
    assert records[0].toudang_plan_count == 1
    assert records[0].min_rank == 41446


def test_split_major_name_collects_all_bracket_notes():
    assert split_major_name(f"土木工程(高校专项计划){_FW_LEFT}要求高考成绩不低于525分{_FW_RIGHT}") == (
        "土木工程",
        f"高校专项计划{_FW_SEMICOLON}要求高考成绩不低于525分",
    )
    assert split_major_name("会计学") == ("会计学", None)


def test_parse_segment_rows_reads_total_columns_from_xls_grid():
    records = parse_segment_rows(_SEGMENT_XLS)

    assert [(r.score, r.segment_count, r.cumulative_count) for r in records] == [
        (697, 7, 56),
        (696, 3, 59),
        (150, 54, 708817),
    ]


def test_read_table_rows_expands_html_export_and_finds_total_last():
    records = parse_segment_rows(read_table_rows(_SEGMENT_HTML_2020))

    assert [(r.score, r.segment_count, r.cumulative_count) for r in records] == [(696, 9, 56), (695, 5, 61)]
