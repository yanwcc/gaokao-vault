from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from scrapling.parser import Adaptor

from gaokao_vault.config import DatabaseConfig
from gaokao_vault.spiders.school_spider import (
    LIST_PAGE_MAX_RETRIES,
    LIST_PAGE_PRIORITY,
    NO_DETAIL_MARKER,
    PAGINATION_PRIORITY,
    SchoolSpider,
    _CaptureSchoolIdsAction,
)


class _Acquire:
    def __init__(self, conn) -> None:
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self, conn) -> None:
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


def _make_school_spider(mode: str = "full") -> SchoolSpider:
    db_config = DatabaseConfig(
        dsn="postgresql://test:test@localhost:5432/test_db",
        pool_min=1,
        pool_max=2,
    )
    return SchoolSpider(db_config=db_config, crawl_task_id=1, mode=mode)


def _make_response(html: str, url: str, meta: dict | None = None) -> MagicMock:
    adaptor = Adaptor(content=html, url=url)
    response = MagicMock()
    response.status = 200
    response.url = url
    response.css = adaptor.css
    response.request = MagicMock()
    response.request.meta = meta or {}
    response.request.url = url
    return response


async def _collect(async_gen) -> list:
    items = []
    async for item in async_gen:
        items.append(item)
    return items


# Mirrors the live list page: cards have no detail href, the click capture
# page_action stamps data-sch-id onto each card before the HTML is captured.
_LIST_URL = "https://gaokao.chsi.com.cn/sch/search--ss-on,option-qg,searchType-1,start-0.dhtml"


def _list_html(cards: list[tuple[str | None, str, str]]) -> str:
    rendered = []
    for sch_id, name, province in cards:
        attr = f' data-sch-id="{sch_id}"' if sch_id is not None else ""
        rendered.append(
            f"""
        <div class="sch-item"{attr}>
          <img src="https://t1.chei.com.cn/common/xh/10001.jpg">
          <div class="info-box">
            <div class="sch-title"><span class="name js-yxk-yxmc"> {name} </span></div>
            <div class="sch-department"><i class="iconfont cursor-pointer">\ue6a4</i>{province}<span class="col-line">|</span><span class="item-depart-title">主管部门\uff1a</span>教育部</div>
          </div>
        </div>"""
        )
    return f"""
<html>
  <body>
    <div id="app-yxk-sch-list"><div class="sch-list-container">{"".join(rendered)}
    </div></div>
  </body>
</html>
"""


_DETAIL_HTML = """
<html>
  <body>
    <div class="content-header">测试大学</div>
    <div class="content-introduction"><span>教育部</span></div>
  </body>
</html>
"""


def test_start_requests_emits_warmup_and_click_capturing_list_entry():
    spider = _make_school_spider()

    requests = asyncio.run(_collect(spider.start_requests()))

    assert len(requests) == 2
    assert requests[0].callback.__name__ == "parse_warmup"
    assert requests[0].url == (
        "https://gaokao.chsi.com.cn/sch/search--ss-on,searchType-1,dataType-2,"
        "schName-,schProvince-,schAddress-,schType-,xlcc-,yxls-,"
        "dual-,naession-,f211-,f985-,autonomy-,central-,start-0.dhtml"
    )
    assert requests[1].url == _LIST_URL
    assert requests[1].callback.__name__ == "parse_school_list"
    assert isinstance(requests[1]._session_kwargs["page_action"], _CaptureSchoolIdsAction)


def test_parse_school_list_schedules_captured_sch_ids_and_next_page():
    spider = _make_school_spider()
    response = _make_response(
        _list_html([("1", "北京大学", "北京"), ("4617101771", "图木舒克职业技术学院", "新疆")]),
        _LIST_URL,
    )

    with patch.object(spider, "_load_province_map", new=AsyncMock(return_value={"北京": 1, "新疆": 31})):
        requests = asyncio.run(_collect(spider.parse_school_list(response)))

    detail_requests = [request for request in requests if "schoolInfoMain--schId-" in request.url]
    pagination_requests = [request for request in requests if request.callback.__name__ == "parse_school_list"]

    assert [request.meta["sch_id"] for request in detail_requests] == [1, 4617101771]
    assert [request.meta["candidate_province_id"] for request in detail_requests] == [1, 31]
    assert all(request.priority == LIST_PAGE_PRIORITY for request in detail_requests)
    assert len(pagination_requests) == 1
    assert pagination_requests[0].url.endswith("searchType-1,start-20.dhtml")
    assert pagination_requests[0].priority == PAGINATION_PRIORITY
    assert isinstance(pagination_requests[0]._session_kwargs["page_action"], _CaptureSchoolIdsAction)


def test_parse_school_list_stops_on_empty_page():
    spider = _make_school_spider()
    response = _make_response(_list_html([]), _LIST_URL.replace("start-0", "start-2960"))

    requests = asyncio.run(_collect(spider.parse_school_list(response)))

    assert requests == []


def test_parse_school_list_retries_page_when_some_cards_were_not_captured():
    spider = _make_school_spider()
    response = _make_response(
        _list_html([("1", "北京大学", "北京"), (None, "清华大学", "北京")]),
        _LIST_URL,
        {"list_retries": 0},
    )

    with patch.object(spider, "_load_province_map", new=AsyncMock(return_value={"北京": 1})):
        requests = asyncio.run(_collect(spider.parse_school_list(response)))

    retry_requests = [request for request in requests if request.url == _LIST_URL]
    assert len(retry_requests) == 1
    assert retry_requests[0].dont_filter is True
    assert retry_requests[0].meta["list_retries"] == 1

    exhausted = _make_response(
        _list_html([(None, "清华大学", "北京")]),
        _LIST_URL,
        {"list_retries": LIST_PAGE_MAX_RETRIES},
    )
    requests = asyncio.run(_collect(spider.parse_school_list(exhausted)))
    assert [request.url for request in requests if request.url == _LIST_URL] == []


def test_parse_school_list_does_not_retry_cards_without_detail_page():
    spider = _make_school_spider()
    response = _make_response(
        _list_html([("1", "北京大学", "北京"), (NO_DETAIL_MARKER, "中国人民大学(苏州校区)", "江苏")]),
        _LIST_URL,
        {"list_retries": 0},
    )

    with patch.object(spider, "_load_province_map", new=AsyncMock(return_value={"北京": 1})):
        requests = asyncio.run(_collect(spider.parse_school_list(response)))

    detail_requests = [request for request in requests if "schoolInfoMain--schId-" in request.url]
    assert [request.meta["sch_id"] for request in detail_requests] == [1]
    assert [request.url for request in requests if request.url == _LIST_URL] == []


def test_parse_school_list_skips_known_schools_in_incremental_mode():
    spider = _make_school_spider(mode="incremental")
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{"sch_id": 1}])
    response = _make_response(
        _list_html([("1", "北京大学", "北京"), ("57290100", "滁州城市职业学院", "安徽")]),
        _LIST_URL,
    )

    with (
        patch.object(spider, "_get_pool", new=AsyncMock(return_value=_FakePool(conn))),
        patch.object(spider, "_resolve_province_id", new=AsyncMock(return_value=12)),
    ):
        requests = asyncio.run(_collect(spider.parse_school_list(response)))

    detail_requests = [request for request in requests if "schoolInfoMain--schId-" in request.url]
    assert [request.meta["sch_id"] for request in detail_requests] == [57290100]


def test_parse_uses_candidate_province_when_detail_page_does_not_resolve_one():
    spider = _make_school_spider()
    response = _make_response(
        _DETAIL_HTML,
        "https://gaokao.chsi.com.cn/sch/schoolInfoMain--schId-12.dhtml",
        {"sch_id": 12, "candidate_province_id": 3},
    )

    with (
        patch.object(spider, "_resolve_province_id", new=AsyncMock(return_value=None)),
        patch.object(spider, "process_item", new=AsyncMock(return_value="new")),
    ):
        items = asyncio.run(_collect(spider.parse(response)))

    assert len(items) == 1
    assert items[0]["province_id"] == 3


def test_parse_keeps_candidate_province_when_detail_resolution_raises():
    spider = _make_school_spider()
    response = _make_response(
        _DETAIL_HTML,
        "https://gaokao.chsi.com.cn/sch/schoolInfoMain--schId-13.dhtml",
        {"sch_id": 13, "candidate_province_id": 5},
    )

    with (
        patch.object(spider, "_resolve_province_id", new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(spider, "process_item", new=AsyncMock(return_value="new")),
    ):
        items = asyncio.run(_collect(spider.parse(response)))

    assert len(items) == 1
    assert items[0]["province_id"] == 5


def test_parse_preserves_existing_province_when_current_item_has_none():
    spider = _make_school_spider()
    response = _make_response(
        _DETAIL_HTML,
        "https://gaokao.chsi.com.cn/sch/schoolInfoMain--schId-14.dhtml",
        {"sch_id": 14, "candidate_province_id": None},
    )
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": 1, "sch_id": 14, "name": "测试大学", "province_id": 7})
    fake_pool = _FakePool(conn)

    with (
        patch.object(spider, "_resolve_province_id", new=AsyncMock(return_value=None)),
        patch.object(spider, "_get_pool", new=AsyncMock(return_value=fake_pool)),
        patch.object(spider, "process_item", new=AsyncMock(return_value="new")),
    ):
        items = asyncio.run(_collect(spider.parse(response)))

    assert len(items) == 1
    assert items[0]["province_id"] == 7


def test_schedule_school_detail_allows_one_candidate_refresh():
    spider = _make_school_spider()

    first = spider._schedule_school_detail(12, None, priority=0)
    second = spider._schedule_school_detail(12, 3, priority=LIST_PAGE_PRIORITY)
    third = spider._schedule_school_detail(12, 4, priority=LIST_PAGE_PRIORITY)

    assert first is not None
    assert first.meta["candidate_province_id"] is None
    assert first.priority == 0
    assert second is not None
    assert second.meta["candidate_province_id"] == 3
    assert second.priority == LIST_PAGE_PRIORITY
    assert second.dont_filter is True
    assert third is None


_DETAIL_WITH_FEATURES_HTML = """
<html>
  <body>
    <div class="content-header">{name}</div>
    <div class="content-introduction">
      <div class="department">教育行政主管部门:<span>{authority}</span></div>
      <div class="yxtx"{box_style}>
        院校特性:
        <span class="syl"{syl_style}>"双一流"建设高校</span>
        <span class="qjjh"{qjjh_style}>强基计划</span>
        <span class="gxzx"{gxzx_style}>高校专项计划</span>
      </div>
    </div>
    <div class="content-info-item">官方电话: <span class="gfdh">{phone}</span></div>
  </body>
</html>
"""


def _parse_detail(spider: SchoolSpider, **fmt) -> dict:
    response = _make_response(
        _DETAIL_WITH_FEATURES_HTML.format(**fmt),
        "https://gaokao.chsi.com.cn/sch/schoolInfoMain--schId-20.dhtml",
        {"sch_id": 20, "candidate_province_id": 1},
    )
    with (
        patch.object(spider, "_resolve_province_id", new=AsyncMock(return_value=1)),
        patch.object(spider, "process_item", new=AsyncMock(return_value="new")),
    ):
        items = asyncio.run(_collect(spider.parse(response)))
    assert len(items) == 1
    return items[0]


def test_parse_keeps_only_feature_tags_the_page_script_left_visible():
    hidden, shown = ' style="display: none;"', ' style="display: inline-block;"'
    item = _parse_detail(
        _make_school_spider(),
        name="北京大学",
        authority="教育部",
        box_style="",
        syl_style=shown,
        qjjh_style=hidden,
        gxzx_style=hidden,
        phone="010-62751407",
    )

    assert item["is_double_first"] is True
    assert item["school_type"] == '"双一流"建设高校'
    assert "强基计划" not in item["introduction"]
    assert item["phone"] == "010-62751407"


def test_parse_hidden_feature_box_means_no_features_and_dash_phone_is_empty():
    item = _parse_detail(
        _make_school_spider(),
        name="山东医学高等专科学校",
        authority="山东省教育厅",
        box_style=' style="display: none;"',
        syl_style="",
        qjjh_style="",
        gxzx_style="",
        phone="-",
    )

    assert item["is_double_first"] is False
    assert item["school_type"] is None
    assert item["authority"] == "山东省教育厅"
    assert item.get("phone") is None


def test_parse_leaves_school_type_empty_when_script_did_not_toggle_tags():
    item = _parse_detail(
        _make_school_spider(),
        name="测试大学",
        authority="教育部",
        box_style="",
        syl_style="",
        qjjh_style="",
        gxzx_style="",
        phone="0531-1234567",
    )

    assert item["school_type"] is None
    assert item["is_double_first"] is False
