from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from scrapling.parser import Adaptor

from gaokao_vault.config import DatabaseConfig
from gaokao_vault.spiders.sdzk import SDZK_ADMISSION_LIST_URL, SdzkAdmissionRecord
from gaokao_vault.spiders.sdzk_official_spider import SdzkOfficialSpider


def _make_spider(mode: str = "full") -> SdzkOfficialSpider:
    db_config = DatabaseConfig(dsn="postgresql://test:test@localhost:5432/test_db", pool_min=1, pool_max=2)
    return SdzkOfficialSpider(db_config=db_config, crawl_task_id=1, mode=mode)


def _make_response(html: str, url: str, meta: dict | None = None) -> MagicMock:
    adaptor = Adaptor(content=html, url=url)
    response = MagicMock()
    response.status = 200
    response.url = url
    response.css = adaptor.css
    response.request = MagicMock()
    response.request.meta = meta or {}
    return response


async def _collect(async_gen) -> list:
    return [item async for item in async_gen]


_LIST_HTML = """
<html><body>
  <form>
    <input type="hidden" name="__VIEWSTATE" value="vs-token">
    <input type="hidden" name="__EVENTVALIDATION" value="ev-token">
  </form>
  <ul>
    <li><a href="NewsInfo.aspx?NewsID=7312">山东省2026年普通类常规批第1次志愿投档情况表<span>2026-07-19</span></a></li>
    <li><a href="NewsInfo.aspx?NewsID=7331">山东省2026年体育类常规批第3次志愿投档情况表<span>2026-08-01</span></a></li>
    <li><a href="NewsInfo.aspx?NewsID=5883">山东省2022年普通类常规批第三次志愿投档情况表<span>2022-08-05</span></a></li>
  </ul>
  <div class="pageS">
    <a href="javascript:__doPostBack('ctl00$Pager$ctl01$ctl01','')">2</a>
    <a href="javascript:__doPostBack('ctl00$Pager$ctl02$ctl00','')">下一页</a>
  </div>
</body></html>
"""


def test_parse_list_requests_admission_notices_in_window_and_next_page():
    spider = _make_spider()
    response = _make_response(_LIST_HTML, SDZK_ADMISSION_LIST_URL, {"list_url": SDZK_ADMISSION_LIST_URL})

    with patch.object(spider, "_crawl_years", return_value=range(2020, 2027)):
        requests = asyncio.run(_collect(spider.parse_list(response)))

    notices = [r for r in requests if r.callback.__name__ == "parse_notice"]
    assert [(r.meta["year"], r.meta["admission_round"]) for r in notices] == [(2026, 1), (2022, 3)]
    assert notices[0].url == "https://www.sdzk.cn/NewsInfo.aspx?NewsID=7312"

    next_pages = [r for r in requests if r.callback.__name__ == "parse_list"]
    assert len(next_pages) == 1
    form = next_pages[0]._session_kwargs["data"]
    assert next_pages[0]._session_kwargs["method"] == "POST"
    assert form["__EVENTTARGET"] == "ctl00$Pager$ctl02$ctl00"
    assert form["__VIEWSTATE"] == "vs-token"
    assert form["__EVENTVALIDATION"] == "ev-token"


def test_parse_list_stops_paging_once_page_is_older_than_window():
    spider = _make_spider(mode="incremental")
    response = _make_response(_LIST_HTML, SDZK_ADMISSION_LIST_URL, {"list_url": SDZK_ADMISSION_LIST_URL})

    with patch.object(spider, "_crawl_years", return_value=range(2027, 2030)):
        requests = asyncio.run(_collect(spider.parse_list(response)))

    assert requests == []


def test_parse_notice_follows_xls_attachments_with_kind_callback():
    spider = _make_spider()
    html = """
    <html><body>
      <a href="/Floadup/file/20260719/639.xls">附件</a>
      <a href="/Floadup/file/20260719/639.pdf">说明</a>
    </body></html>
    """
    meta = {"kind": "segment", "title": "2026年夏季高考文化成绩一分一段表", "year": 2026}
    response = _make_response(html, "https://www.sdzk.cn/NewsInfo.aspx?NewsID=7258", meta)

    requests = asyncio.run(_collect(spider.parse_notice(response)))

    assert [r.url for r in requests] == ["https://www.sdzk.cn/Floadup/file/20260719/639.xls"]
    assert requests[0].callback.__name__ == "parse_segment_file"
    assert requests[0].meta["notice_url"] == "https://www.sdzk.cn/NewsInfo.aspx?NewsID=7258"


def test_admission_item_data_links_school_with_normalised_brackets():
    record = SdzkAdmissionRecord(
        education_level=None,
        school_code_raw="A433",
        school_name_raw="中国石油大学(华东)",
        major_code_raw="0C",
        major_name_raw="石油工程(高校专项计划)",
        major_name="石油工程",
        major_note="高校专项计划",
        selection_requirement="物理、化学",
        min_rank=35120,
        min_rank_raw=None,
        min_score=None,
        plan_count=None,
        toudang_plan_count=None,
        admitted_count=None,
    )
    meta = {"year": 2026, "admission_round": 1, "notice_url": "https://www.sdzk.cn/NewsInfo.aspx?NewsID=7312"}

    data = SdzkOfficialSpider._admission_item_data(record, meta, 15, {"中国石油大学(华东)": 7})

    assert data["school_id"] == 7
    assert data["province_id"] == 15
    assert data["batch"] == "常规批"
    assert data["program_type"] == "专项计划"
    assert data["source_url"] == "https://www.sdzk.cn/NewsInfo.aspx?NewsID=7312"
    assert data["data_source"] == "sdzk.cn"


def _record(education_level: str | None, school_name: str = "测试大学") -> SdzkAdmissionRecord:
    return SdzkAdmissionRecord(
        education_level=education_level,
        school_code_raw="A999",
        school_name_raw=school_name,
        major_code_raw="01",
        major_name_raw="护理",
        major_name="护理",
        major_note=None,
        selection_requirement="不限",
        min_rank=300000,
        min_rank_raw=None,
        min_score=None,
        plan_count=None,
        toudang_plan_count=None,
        admitted_count=None,
    )


def test_admission_item_data_falls_back_to_school_level_when_table_has_none():
    meta = {"year": 2026, "admission_round": 1, "notice_url": "https://www.sdzk.cn/NewsInfo.aspx?NewsID=1"}

    data = SdzkOfficialSpider._admission_item_data(_record(None), meta, 15, {"测试大学": 7}, {7: "本科"})

    assert data["education_level"] == "本科"


def test_admission_item_data_keeps_the_row_level_over_the_school_level():
    meta = {"year": 2026, "admission_round": 2, "notice_url": "https://www.sdzk.cn/NewsInfo.aspx?NewsID=2"}

    data = SdzkOfficialSpider._admission_item_data(_record("专科"), meta, 15, {"测试大学": 7}, {7: "本科"})

    assert data["education_level"] == "专科"


def test_admission_item_data_leaves_level_empty_for_unlinked_schools():
    meta = {"year": 2026, "admission_round": 1, "notice_url": "https://www.sdzk.cn/NewsInfo.aspx?NewsID=3"}

    data = SdzkOfficialSpider._admission_item_data(
        _record(None, "香港珠海学院"), meta, 15, {"测试大学": 7}, {7: "本科"}
    )

    assert data["school_id"] is None
    assert data["education_level"] is None
