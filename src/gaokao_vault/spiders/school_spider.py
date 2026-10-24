from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, ClassVar

from scrapling.spiders import Request, Response

from gaokao_vault.constants import BASE_URL, TaskType
from gaokao_vault.db.queries.schools import find_school_by_sch_id, upsert_school
from gaokao_vault.models.school import SchoolItem
from gaokao_vault.pipeline.validator import validate_item
from gaokao_vault.spiders.base import BaseGaokaoSpider

logger = logging.getLogger(__name__)

SEARCH_ENTRY_URL = f"{BASE_URL}/sch/search--ss-on,option-qg,searchType-1,start-0.dhtml"
_SCH_ID_PATTERN = re.compile(r"schoolInfo(?:Main)?--schId-(\d*)")
# Some cards (e.g. 中国人民大学(苏州校区)) link to "schoolInfo--schId-.dhtml": the site has no detail page.
NO_DETAIL_MARKER = "none"
# Detail pages render a missing phone / website as "-".
_EMPTY_PLACEHOLDERS = frozenset({"-", "--"})
_HIDDEN_STYLE_PATTERN = re.compile(r"display\s*:\s*none", re.IGNORECASE)


def card_level(school_card: Any) -> str | None:
    """Education level from a list card's tags: "本科" or "专科" (shown as "高职(专科)")."""
    tags = ["".join(tag.css("::text").getall()).strip() for tag in school_card.css(".sch-level-tag")]
    if "本科" in tags:
        return "本科"
    if any("专科" in tag for tag in tags):
        return "专科"
    return None


def _is_hidden(node: Any) -> bool:
    return bool(_HIDDEN_STYLE_PATTERN.search(node.attrib.get("style", "")))


_LIST_START_PATTERN = re.compile(r"start-(\d+)\.dhtml")
LIST_PAGE_SIZE = 20
LIST_PAGE_MAX_RETRIES = 2
LIST_PAGE_PRIORITY = 10
PAGINATION_PRIORITY = 10
_CLICK_CAPTURE_POLLS = 40
_CLICK_CAPTURE_POLL_SECONDS = 0.05


class _CaptureSchoolIdsAction:
    """Pickle-safe page_action that resolves the schId of every card on a school list page.

    List cards carry no detail href: the schId only shows up in the URL the card's click
    handler opens, and newer schools use sparse ids (e.g. 7713355113) that cannot be
    enumerated. Each card is clicked, the popup's detail navigation is intercepted and
    aborted, and the schId is stamped onto the card as ``data-sch-id`` before the page
    HTML is captured.
    """

    async def __call__(self, page):
        context = page.context
        captured: list[str] = []
        popups: list[Any] = []

        def on_page(popup) -> None:
            popups.append(popup)

        async def intercept(route) -> None:
            if await self._is_popup_navigation(route, page):
                captured.append(route.request.url)
                await route.abort()
            else:
                await route.continue_()

        context.on("page", on_page)
        await context.route(_SCH_ID_PATTERN, intercept)
        try:
            names = page.locator("div.sch-item .js-yxk-yxmc")
            for index in range(await names.count()):
                await self._stamp_card(names.nth(index), captured)
        finally:
            await context.unroute(_SCH_ID_PATTERN, intercept)
            context.remove_listener("page", on_page)
            for popup in popups:
                if not popup.is_closed() and await popup.opener() is page:
                    await popup.close()
        return page

    @staticmethod
    async def _is_popup_navigation(route, page) -> bool:
        try:
            source_page = route.request.frame.page
        except Exception:
            # A popup's first navigation is issued before its frame exists; only the
            # card clicks open popups, other spider tabs navigate existing frames.
            return True
        return source_page is not page and await source_page.opener() is page

    @staticmethod
    async def _stamp_card(name_locator, captured: list[str]) -> None:
        before = len(captured)
        await name_locator.click()
        for _ in range(_CLICK_CAPTURE_POLLS):
            if len(captured) > before:
                break
            await asyncio.sleep(_CLICK_CAPTURE_POLL_SECONDS)
        else:
            return
        match = _SCH_ID_PATTERN.search(captured[-1])
        if match:
            await name_locator.evaluate(
                "(el, schId) => el.closest('.sch-item').setAttribute('data-sch-id', schId)",
                match.group(1) or NO_DETAIL_MARKER,
            )


class SchoolSpider(BaseGaokaoSpider):
    name: str = "school_spider"
    task_type: str = TaskType.SCHOOLS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._province_map: dict[str, int] | None = None
        self._scheduled_school_candidates: dict[int, int | None] = {}
        self._refreshed_school_ids: set[int] = set()
        self._visited_list_urls: set[str] = set()
        self._known_sch_ids: set[int] | None = None

    async def _load_province_map(self) -> dict[str, int]:
        async with (await self._get_pool()).acquire() as conn:
            rows = await conn.fetch("SELECT id, name FROM provinces")
        return {row["name"]: row["id"] for row in rows}

    async def _load_known_sch_ids(self) -> set[int]:
        """In incremental mode, schools already stored are not re-fetched."""
        if self._known_sch_ids is None:
            if self.mode == "incremental":
                async with (await self._get_pool()).acquire() as conn:
                    rows = await conn.fetch("SELECT sch_id FROM schools")
                self._known_sch_ids = {int(row["sch_id"]) for row in rows}
            else:
                self._known_sch_ids = set()
        return self._known_sch_ids

    async def _resolve_province_id(self, province_text: str | None) -> int | None:
        if not province_text:
            return None

        if self._province_map is None:
            self._province_map = await self._load_province_map()

        normalized = (
            province_text
            .strip()
            .replace("壮族自治区", "")
            .replace("回族自治区", "")
            .replace("维吾尔自治区", "")
            .replace("自治区", "")
        )
        simplified = normalized.replace("省", "").replace("市", "")

        for name, province_id in self._province_map.items():
            plain_name = name.replace("省", "").replace("市", "")
            if name in normalized or plain_name in normalized or plain_name == simplified:
                return province_id

        return None

    def _schedule_school_detail(
        self,
        sch_id: int,
        candidate_province_id: int | None,
        *,
        priority: int = 0,
        level: str | None = None,
    ) -> Request | None:
        if sch_id not in self._scheduled_school_candidates:
            self._scheduled_school_candidates[sch_id] = candidate_province_id
            return Request(
                f"{BASE_URL}/sch/schoolInfoMain--schId-{sch_id}.dhtml",
                callback=self.parse,
                priority=priority,
                meta={"sch_id": sch_id, "candidate_province_id": candidate_province_id, "level": level},
            )

        if (
            candidate_province_id is not None
            and self._scheduled_school_candidates[sch_id] is None
            and sch_id not in self._refreshed_school_ids
        ):
            self._scheduled_school_candidates[sch_id] = candidate_province_id
            self._refreshed_school_ids.add(sch_id)
            return Request(
                f"{BASE_URL}/sch/schoolInfoMain--schId-{sch_id}.dhtml",
                callback=self.parse,
                priority=priority,
                dont_filter=True,
                meta={"sch_id": sch_id, "candidate_province_id": candidate_province_id, "level": level},
            )

        return None

    async def _preserve_existing_province_id(self, item: dict[str, Any]) -> dict[str, Any]:
        if item.get("province_id") is not None:
            return item

        async with (await self._get_pool()).acquire() as conn:
            existing = await find_school_by_sch_id(conn, item["sch_id"])
        if existing and existing.get("province_id") is not None:
            item["province_id"] = existing["province_id"]
        return item

    @staticmethod
    def _warmup_url() -> str:
        return (
            f"{BASE_URL}/sch/search--ss-on,searchType-1,dataType-2,"
            "schName-,schProvince-,schAddress-,schType-,xlcc-,yxls-,"
            "dual-,naession-,f211-,f985-,autonomy-,central-,start-0.dhtml"
        )

    async def start_requests(self):
        # Warmup: visit the list page first to establish Cookie/session state
        yield Request(self._warmup_url(), callback=self.parse_warmup)
        self._visited_list_urls.add(SEARCH_ENTRY_URL)
        yield self._list_page_request(SEARCH_ENTRY_URL, priority=PAGINATION_PRIORITY)

    def _list_page_request(self, url: str, *, priority: int, retries: int = 0) -> Request:
        return Request(
            url,
            callback=self.parse_school_list,
            priority=priority,
            dont_filter=retries > 0,
            meta={"list_retries": retries},
            page_action=_CaptureSchoolIdsAction(),
        )

    @staticmethod
    def _next_list_page_url(url: str) -> str | None:
        match = _LIST_START_PATTERN.search(url)
        if match is None:
            return None
        next_start = int(match.group(1)) + LIST_PAGE_SIZE
        return f"{url[: match.start()]}start-{next_start}.dhtml{url[match.end() :]}"

    async def parse_warmup(self, response):
        """Handle warmup response — just log and return."""
        logger.info("Warmup request completed: status=%s url=%s", response.status, response.url)
        return
        yield  # make this an async generator to satisfy Request callback type

    async def _resolve_card_province_id(self, school_card, sch_id: int) -> int | None:
        department = school_card.css(".sch-department").first
        if not department:
            return None

        # Card text looks like "<icon>新疆|主管部门..."; the province is the part before "|".
        department_text = "".join(department.css("::text").getall())
        province_text = "".join(ch for ch in department_text.split("|", 1)[0] if ch.isprintable()).strip()
        if not province_text:
            return None

        try:
            return await self._resolve_province_id(province_text)
        except Exception:
            logger.warning(
                "Failed to resolve province from school list card schId=%s text=%s",
                sch_id,
                province_text,
                exc_info=True,
            )
            return None

    @staticmethod
    def _card_sch_id(school_card, list_url: str) -> tuple[bool, int | None]:
        """Return (resolved, schId) for a list card; cards without a detail page resolve to None."""
        raw_sch_id = school_card.attrib.get("data-sch-id", "").strip()
        if raw_sch_id == NO_DETAIL_MARKER:
            card_name = "".join(school_card.css(".js-yxk-yxmc ::text").getall()).strip()
            logger.info("School list card has no detail page on the site: %s (%s)", card_name, list_url)
            return True, None
        if not raw_sch_id.isdigit():
            return False, None
        return True, int(raw_sch_id)

    async def parse_school_list(self, response: Response):
        if response.request is None:
            return

        school_cards = response.css("div.sch-item")
        if not school_cards:
            logger.info("School list exhausted at %s", response.url)
            return

        known_sch_ids = await self._load_known_sch_ids()
        resolved = 0
        for school_card in school_cards:
            card_resolved, sch_id = self._card_sch_id(school_card, response.url)
            resolved += card_resolved
            if sch_id is None or sch_id in known_sch_ids:
                continue

            candidate_province_id = await self._resolve_card_province_id(school_card, sch_id)
            request = self._schedule_school_detail(
                sch_id, candidate_province_id, priority=LIST_PAGE_PRIORITY, level=card_level(school_card)
            )
            if request is not None:
                yield request

        if resolved < len(school_cards):
            retries = int(response.request.meta.get("list_retries", 0))
            logger.warning(
                "Resolved %d/%d schIds on %s (retry %d/%d)",
                resolved,
                len(school_cards),
                response.url,
                retries,
                LIST_PAGE_MAX_RETRIES,
            )
            if retries < LIST_PAGE_MAX_RETRIES:
                yield self._list_page_request(response.url, priority=PAGINATION_PRIORITY, retries=retries + 1)

        next_url = self._next_list_page_url(response.url)
        if next_url is None or next_url in self._visited_list_urls:
            return
        self._visited_list_urls.add(next_url)
        yield self._list_page_request(next_url, priority=PAGINATION_PRIORITY)

    async def parse(self, response: Response):
        if response.status == 404:
            return

        if response.request is None:
            return
        sch_id = response.request.meta.get("sch_id", 0)
        candidate_province_id = response.request.meta.get("candidate_province_id")

        name = self._extract_school_name(response)
        if not name:
            logger.debug("No school name found for schId=%d", sch_id)
            return

        data: dict[str, Any] = {"sch_id": sch_id, "name": name}

        self._extract_detail_fields(response, data)
        self._extract_tags(response, data)
        self._extract_logo_and_intro(response, data)
        try:
            detail_province_id = await self._resolve_province_id(data.get("city"))
        except Exception:
            logger.warning("Failed to resolve province for schId=%s", sch_id, exc_info=True)
            detail_province_id = None
        data["province_id"] = detail_province_id or candidate_province_id
        if level := response.request.meta.get("level"):
            data["level"] = level

        item = validate_item(SchoolItem, data)
        if item:
            item = await self._preserve_existing_province_id(item)
            yield item
            await self.process_item(
                item,
                entity_type="schools",
                unique_keys={"sch_id": sch_id},
                upsert_fn=upsert_school,
            )

    @staticmethod
    def _extract_school_name(response: Response) -> str:
        name_el = response.css("div.content-header")
        if not name_el:
            return ""
        for t in name_el.css("::text").getall():
            t = t.strip()
            if t and "关注" not in t and not t.isdigit():
                return t
        return ""

    @staticmethod
    def _visible_feature_tags(response: Response) -> list[str] | None:
        """院校特性 tags the page script left visible.

        The template always renders every tag (双一流 / 强基计划 / 高校专项计划); the script hides the
        container or individual spans with ``display: none``. Returns None when no span carries a
        style, i.e. the script never toggled them and the tags cannot be trusted.
        """
        box = response.css("div.content-introduction .yxtx").first
        if not box or _is_hidden(box):
            return []
        spans = box.css("span")
        if spans and not any("style" in span.attrib for span in spans):
            return None
        return [
            text for span in spans if not _is_hidden(span) and (text := "".join(span.css("::text").getall()).strip())
        ]

    @staticmethod
    def _non_feature_intro_spans(response: Response) -> list[str]:
        texts = []
        for span in response.css("div.content-introduction span"):
            parent = span.parent
            if parent is not None and "yxtx" in parent.attrib.get("class", "").split():
                continue
            text = "".join(span.css("::text").getall()).strip()
            if text:
                texts.append(text)
        return texts

    def _extract_tags(self, response: Response, data: dict) -> None:
        tag_set = set(self._non_feature_intro_spans(response)) | set(self._visible_feature_tags(response) or [])
        data["is_211"] = "211" in tag_set
        data["is_985"] = "985" in tag_set
        data["is_double_first"] = any("双一流" in t for t in tag_set)
        data["is_private"] = "民办" in tag_set
        data["is_independent"] = "独立学院" in tag_set
        data["is_sino_foreign"] = "中外合作办学" in tag_set

    @staticmethod
    def _extract_logo_and_intro(response: Response, data: dict) -> None:
        logo_el = response.css("div.yxxx-header-img img::attr(src)")
        if logo_el:
            data["logo_url"] = logo_el.get("")

        intro_el = response.css("div.content-introduction").first
        if intro_el:
            feature_texts = {t.strip() for t in intro_el.css(".yxtx ::text").getall() if t.strip()}
            parts = [t.strip() for t in intro_el.css("::text").getall() if t.strip() and t.strip() not in feature_texts]
            visible_tags = SchoolSpider._visible_feature_tags(response)
            if visible_tags:
                parts += ["院校特性:", *visible_tags]
            full_intro = " ".join(parts)
            if full_intro:
                data["introduction"] = full_intro[:5000]

    _SPAN_FIELD_MAP: ClassVar[dict[str, str]] = {
        "yxszd": "city",
        "txdz": "address",
        "gfdh": "phone",
    }

    _LINK_FIELD_MAP: ClassVar[dict[str, str]] = {
        "gfwz": "website",
        "zswz": "recruit_website",
    }

    _TEXT_FIELD_MAP: ClassVar[dict[str, str]] = {
        "教育行政主管部门": "authority",
        "隶属于": "authority",
        "院校特性": "school_type",
        "院校类型": "school_type",
    }

    def _extract_school_type(self, response: Response, data: dict) -> None:
        feature_tags = self._visible_feature_tags(response)
        if feature_tags is None:
            logger.warning(
                "院校特性 tags were not toggled by the page script for schId=%s; leaving school_type empty",
                data.get("sch_id"),
            )
        elif feature_tags:
            data["school_type"] = " | ".join(feature_tags)

    def _extract_detail_fields(self, response: Response, data: dict) -> None:
        """Extract fields from div.content-info-item using span classes and text labels."""
        department_texts = [
            text.strip()
            for text in response.css("div.content-introduction .department span::text").getall()
            if text.strip()
        ]
        if department_texts:
            data["authority"] = department_texts[-1]

        self._extract_school_type(response, data)

        for css_cls, field in self._SPAN_FIELD_MAP.items():
            el = response.css(f"span.{css_cls}::text")
            value = el.get("").strip() if el else ""
            if value and value not in _EMPTY_PLACEHOLDERS:
                data[field] = value

        for css_cls, field in self._LINK_FIELD_MAP.items():
            el = response.css(f"a.{css_cls}::attr(href)")
            value = el.get("").strip() if el else ""
            if value and value not in _EMPTY_PLACEHOLDERS:
                data[field] = value

        for info_item in response.css("div.content-info-item"):
            full_text = " ".join(t.strip() for t in info_item.css("::text").getall())
            for label, field in self._TEXT_FIELD_MAP.items():
                if label in full_text:
                    self._extract_labeled_span(info_item, label, field, data)

    @staticmethod
    def _extract_labeled_span(info_item, label: str, field: str, data: dict) -> None:
        for s in info_item.css("span::text").getall():
            s = s.strip()
            if s and s != label:
                data[field] = s
                return
