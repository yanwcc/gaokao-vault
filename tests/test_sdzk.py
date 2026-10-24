from __future__ import annotations

from gaokao_vault.spiders.sdzk import (
    SdzkAdmissionTitle,
    parse_admission_rows,
    parse_admission_title,
    parse_score_line_title,
    parse_segment_rows,
    parse_segment_title,
    parse_spring_score_line_title,
    parse_spring_score_lines,
    parse_spring_segment_rows,
    parse_spring_segment_title,
    parse_summer_score_lines,
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

_SPRING_ADMISSION_2026 = [
    ["", "山东省2026年春季高考本科批第1次志愿投档情况表", "", "", ""],
    ["", "专业类别", "专业代号及名称", "院校代号及名称", "最低位次"],
    ["", "现代农艺", "20食品工程技术", "C062日照职业技术大学", "65"],
    ["", "烹饪", "21食品工程技术", "C062日照职业技术大学", "88"],
]

_SPRING_ADMISSION_2024 = [
    ["", "山东省2024年春季高考专科批第1次志愿投档情况表", "", "", "", ""],
    ["", "专业类别", "专业代号及名称", "院校代号及名称", "投档计划数", "最低位次"],
    ["", "现代农艺", "B2食品检验检测技术", "C062日照职业技术学院", "20", "194"],
]

_SPRING_SEGMENT = [
    ["2026年春季高考成绩一分一段表", "", "", "", ""],
    ["专业类别代码", "专业类别名称", "分数段", "本段人数", "累计人数"],
    ["01", "现代农艺", "635", "1", "1"],
    ["01", "现代农艺", "624", "1", "2"],
    ["30", "艺术设计", "0", "42", "4979"],
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


def test_parse_admission_title_reads_spring_batches_with_their_level():
    assert parse_admission_title("山东省2026年春季高考本科批第1次志愿投档情况表") == SdzkAdmissionTitle(
        2026, 1, "春季本科批", "本科"
    )
    assert parse_admission_title("山东省2022年春季高考专科批第二次志愿投档情况表") == SdzkAdmissionTitle(
        2022, 2, "春季专科批", "专科"
    )
    assert parse_admission_title("山东省2026年春季高考专科批第2次志愿院校专业计划") is None
    assert parse_admission_title("山东省2020年春季高考本科批第1次志愿院校投档情况统计表") is None


def test_parse_spring_segment_title():
    assert parse_spring_segment_title("2026年春季高考成绩一分一段表") == 2026
    assert parse_spring_segment_title("2021年春季高考一分一段表") == 2021
    assert parse_spring_segment_title("2026年夏季高考文化成绩一分一段表") is None
    assert parse_spring_segment_title("春季高考本科首次志愿录取后一分一段表") is None


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


def test_parse_admission_rows_reads_spring_category_without_mistaking_it_for_the_major():
    records = parse_admission_rows(_SPRING_ADMISSION_2026)

    assert [(r.category, r.major_code_raw, r.major_name, r.min_rank) for r in records] == [
        ("现代农艺", "20", "食品工程技术", 65),
        ("烹饪", "21", "食品工程技术", 88),
    ]
    assert records[0].school_code_raw == "C062"
    assert records[0].education_level is None


def test_parse_admission_rows_reads_spring_plan_count():
    records = parse_admission_rows(_SPRING_ADMISSION_2024)

    assert len(records) == 1
    assert (records[0].category, records[0].toudang_plan_count, records[0].min_rank) == ("现代农艺", 20, 194)


def test_parse_admission_rows_leaves_summer_category_empty():
    assert {record.category for record in parse_admission_rows(_ADMISSION_2026)} == {None}


def test_parse_spring_segment_rows_keeps_category_per_row():
    records = parse_spring_segment_rows(_SPRING_SEGMENT)

    assert [(r.category, r.score, r.segment_count, r.cumulative_count) for r in records] == [
        ("现代农艺", 635, 1, 1),
        ("现代农艺", 624, 1, 2),
        ("艺术设计", 0, 42, 4979),
    ]


# Text as pypdf extracts it from the 2026 PDFs (notes trimmed): table cells follow the section
# headings, and the 体育类 table repeats 一段线/二段线 after "综合分".
_SUMMER_LINES_TEXT = """山东省 2026 年夏季高考
各类别分数线
一、普通类
三、体育类
分 数
特殊类型招生控制线 525
一段线 442
二段线 150
“3+2 对口贯通分段培养”
高职志愿填报资格线 392
本科文化控制线 442 331 287
专科文化控制线 150
综合分
一段线 574
二段线 444"""

_SPRING_LINES_TEXT = """1
山东省 2026 年春季高考各专业类别
录取控制线
一、本科录取控制线
专业类别代码 专业类别 分数线
01 现代农艺 527
19 药学 525
2
20 护理 520
30 艺术设计 515
二、技能拔尖人才本科录取控制线 140 分
四、专科录取控制线
各专业类别专科录取控制线均为 150 分"""


def test_parse_score_line_titles():
    assert parse_score_line_title("山东省2026年夏季高考各类别分数线") == 2026
    assert parse_spring_score_line_title("山东省2025年春季高考各专业类别录取控制线") == 2025
    assert parse_spring_score_line_title("山东省2026年春季高考本科批第2次志愿录取控制线") is None
    assert parse_spring_score_line_title("山东省2022年春季高考各专业类别本科录取控制线") is None


def test_parse_summer_score_lines_takes_the_general_table_not_the_sports_one():
    lines = parse_summer_score_lines(_SUMMER_LINES_TEXT, "综合")

    assert [(line.batch, line.name, line.score, line.category) for line in lines] == [
        ("常规批", "特殊类型招生控制线", 525, "综合"),
        ("常规批", "一段线", 442, "综合"),
        ("常规批", "二段线", 150, "综合"),
    ]


def test_parse_summer_score_lines_rejects_out_of_order_scores():
    assert parse_summer_score_lines(_SUMMER_LINES_TEXT.replace("一段线 442", "一段线 100", 1), "综合") == []


def test_parse_spring_score_lines_expands_the_shared_zhuanke_line_per_category():
    lines = parse_spring_score_lines(_SPRING_LINES_TEXT)

    benke = [(line.category, line.score) for line in lines if line.batch == "春季本科批"]
    zhuanke = [(line.category, line.score) for line in lines if line.batch == "春季专科批"]
    assert benke == [("现代农艺", 527), ("药学", 525), ("护理", 520), ("艺术设计", 515)]
    assert zhuanke == [("现代农艺", 150), ("药学", 150), ("护理", 150), ("艺术设计", 150)]
    assert {line.name for line in lines} == {"录取控制线"}
