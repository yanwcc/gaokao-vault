from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

from scrapling.fetchers import FetcherSession
from scrapling.spiders import Request, Response

from gaokao_vault.constants import TaskType
from gaokao_vault.db.queries.admission import upsert_provincial_admission_record
from gaokao_vault.db.queries.scores import upsert_score_line, upsert_score_segment
from gaokao_vault.models.admission import ProvincialAdmissionRecordItem
from gaokao_vault.models.score import ScoreLineItem, ScoreSegmentItem
from gaokao_vault.pipeline.admission_rules import extract_program_type
from gaokao_vault.pipeline.validator import validate_item
from gaokao_vault.spiders.base import BaseGaokaoSpider
from gaokao_vault.spiders.scope import iter_crawl_years
from gaokao_vault.spiders.sdzk import (
    ADMISSION_BATCH,
    SDZK_ADMISSION_LIST_URL,
    SDZK_BASE_URL,
    SDZK_DATA_SOURCE,
    SDZK_SCORE_LINE_LIST_URL,
    SDZK_SEGMENT_LIST_URL,
    SDZK_SPRING_ADMISSION_LIST_URL,
    SDZK_SPRING_SCORE_LINE_LIST_URL,
    SDZK_SPRING_SEGMENT_LIST_URL,
    SPRING_BATCH_PREFIX,
    SdzkAdmissionRecord,
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
    read_pdf_text,
    read_table_rows,
)

logger = logging.getLogger(__name__)

SHANDONG_PROVINCE_CODE = "37"
# Shandong switched to the 3+3 new gaokao (no 文/理 split) in 2020.
_YEAR_START = 2020
# Spring 投档 tables list majors from 2023 on; earlier ones stop at school x 专业类别, and 2020-2021 still
# used the pre-2022 categories. 2022 has no spring segment table on the site.
_SPRING_YEAR_START = 2023
# Score line notices are PDFs with a text layer from 2025 on; earlier years are published as images.
_SCORE_LINE_YEAR_START = 2025
_ATTACHMENT_SUFFIX = {"score_line": ".pdf", "spring_score_line": ".pdf"}
_SEGMENT_SUBJECT_CATEGORY = "综合"
_TITLE_DATE_SUFFIX = re.compile(r"\s*\d{4}-\d{2}-\d{2}$")
_TITLE_YEAR = re.compile(r"(\d{4})年")
_NEXT_PAGE_POSTBACK = re.compile(r"__doPostBack\('([^']+)'")


def _normalize_school_name(name: str) -> str:
    return re.sub(r"\s+", "", name).replace(chr(0xFF08), "(").replace(chr(0xFF09), ")")


class SdzkOfficialSpider(BaseGaokaoSpider):
    """Import Shandong's official admission (投档情况表), score segment (一分一段表) and batch score line
    (分数线 / 录取控制线) publications from sdzk.cn.

    Covers the summer gaokao and the spring gaokao (春季高考); spring rows carry their 专业类别.
    """

    name: str = "sdzk_official_spider"
    task_type: str = TaskType.SDZK_OFFICIAL
    allowed_domains = {"www.sdzk.cn"}  # noqa: RUF012

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._province_id: int | None = None
        self._school_ids: dict[str, int] | None = None
        self._school_levels: dict[int, str] = {}

    def configure_sessions(self, manager) -> None:
        manager.add("http", FetcherSession())

    def _crawl_years(self) -> range:
        return iter_crawl_years(mode=self.mode, full_start_year=_YEAR_START, current_year=datetime.now().year)

    async def start_requests(self):
        for list_url in (
            SDZK_ADMISSION_LIST_URL,
            SDZK_SEGMENT_LIST_URL,
            SDZK_SPRING_ADMISSION_LIST_URL,
            SDZK_SPRING_SEGMENT_LIST_URL,
            SDZK_SCORE_LINE_LIST_URL,
            SDZK_SPRING_SCORE_LINE_LIST_URL,
        ):
            yield Request(list_url, callback=self.parse_list, meta={"list_url": list_url})

    async def parse_list(self, response: Response):
        if response.request is None:
            return
        list_url = response.request.meta["list_url"]
        years = self._crawl_years()

        page_years: list[int] = []
        for link in response.css("a[href*='NewsInfo.aspx?NewsID=']"):
            title = _TITLE_DATE_SUFFIX.sub("", "".join(link.css("::text").getall()).strip())
            if year_match := _TITLE_YEAR.search(title):
                page_years.append(int(year_match.group(1)))
            request = self._notice_request(link.attrib.get("href", ""), title, years)
            if request is not None:
                yield request

        # Lists are newest first; stop paging once a page is entirely older than the crawl window.
        if page_years and max(page_years) < years.start:
            return
        next_page = self._next_page_request(response, list_url)
        if next_page is not None:
            yield next_page

    def _notice_request(self, href: str, title: str, years: range) -> Request | None:
        if not href:
            return None
        url = urljoin(f"{SDZK_BASE_URL}/", href)
        spring_years = range(max(years.start, _SPRING_YEAR_START), years.stop)

        admission = parse_admission_title(title)
        if admission is not None:
            is_spring = admission.batch.startswith(SPRING_BATCH_PREFIX)
            if admission.year not in (spring_years if is_spring else years):
                return None
            return Request(
                url,
                callback=self.parse_notice,
                meta={
                    "kind": "admission",
                    "title": title,
                    "year": admission.year,
                    "batch": admission.batch,
                    "admission_round": admission.admission_round,
                    "education_level": admission.education_level,
                },
            )

        segment_year = parse_segment_title(title)
        if segment_year is not None and segment_year in years:
            return Request(
                url, callback=self.parse_notice, meta={"kind": "segment", "title": title, "year": segment_year}
            )
        spring_segment_year = parse_spring_segment_title(title)
        if spring_segment_year is not None and spring_segment_year in spring_years:
            return Request(
                url,
                callback=self.parse_notice,
                meta={"kind": "spring_segment", "title": title, "year": spring_segment_year},
            )

        line_years = range(max(years.start, _SCORE_LINE_YEAR_START), years.stop)
        for kind, line_year in (
            ("score_line", parse_score_line_title(title)),
            ("spring_score_line", parse_spring_score_line_title(title)),
        ):
            if line_year is not None and line_year in line_years:
                return Request(url, callback=self.parse_notice, meta={"kind": kind, "title": title, "year": line_year})
        return None

    def _next_page_request(self, response: Response, list_url: str) -> Request | None:
        """The list pager is an ASP.NET postback; replay it with the page's hidden form state."""
        target = None
        for link in response.css("a[href^='javascript:__doPostBack']"):
            if "".join(link.css("::text").getall()).strip() == "下一页":
                match = _NEXT_PAGE_POSTBACK.search(link.attrib.get("href", ""))
                target = match.group(1) if match else None
                break
        if target is None:
            return None

        form: dict[str, Any] = {
            field.attrib.get("name", ""): field.attrib.get("value", "")
            for field in response.css("input[type='hidden'][name^='__']")
        }
        form["__EVENTTARGET"] = target
        form["__EVENTARGUMENT"] = ""
        return Request(
            list_url,
            callback=self.parse_list,
            method="POST",
            data=form,
            meta={"list_url": list_url},
        )

    async def parse_notice(self, response: Response):
        if response.request is None:
            return
        meta = response.request.meta
        suffix = _ATTACHMENT_SUFFIX.get(meta["kind"], ".xls")
        attachments = [
            urljoin(SDZK_BASE_URL, href)
            for href in response.css("a[href*='/Floadup/']::attr(href)").getall()
            if href.lower().endswith(suffix)
        ]
        if not attachments:
            logger.warning("No %s attachment on sdzk notice %s (%s)", suffix, response.url, meta.get("title"))
            return

        if meta["kind"] == "admission":
            callback = self.parse_admission_file
        elif meta["kind"] in _ATTACHMENT_SUFFIX:
            callback = self.parse_score_line_file
        else:
            callback = self.parse_segment_file
        for url in attachments:
            yield Request(url, callback=callback, meta={**meta, "notice_url": response.url})

    async def parse_admission_file(self, response: Response):
        if response.request is None:
            return
        meta = response.request.meta
        records = parse_admission_rows(read_table_rows(response.body))
        if not records:
            logger.warning("No admission rows parsed from %s (%s)", response.url, meta.get("title"))
            return
        logger.info("Parsed %d admission rows from %s", len(records), meta.get("title"))

        province_id = await self._get_province_id()
        school_ids = await self._get_school_ids()
        for record in records:
            data = self._admission_item_data(record, meta, province_id, school_ids, self._school_levels)
            data["subject_category_id"] = await self._resolve_subject_category(record.category or "")
            item = validate_item(ProvincialAdmissionRecordItem, data)
            if item is None:
                continue
            yield item
            await self.process_item(
                item,
                entity_type="provincial_admission_records",
                unique_keys={
                    "province_id": province_id,
                    "year": item["year"],
                    "batch": item["batch"],
                    "admission_round": item["admission_round"],
                    "school_code_raw": item["school_code_raw"],
                    "major_code_raw": item["major_code_raw"],
                },
                upsert_fn=upsert_provincial_admission_record,
            )

    @staticmethod
    def _admission_item_data(
        record: SdzkAdmissionRecord,
        meta: dict[str, Any],
        province_id: int,
        school_ids: dict[str, int],
        school_levels: dict[int, str] | None = None,
    ) -> dict[str, Any]:
        school_id = school_ids.get(_normalize_school_name(record.school_name_raw))
        school_level = (school_levels or {}).get(school_id) if school_id is not None else None
        return {
            "province_id": province_id,
            "year": meta["year"],
            "batch": meta.get("batch", ADMISSION_BATCH),
            "admission_round": meta["admission_round"],
            # Spring batches are one level each. The summer round-1 table has no level column; fall back to
            # the linked school's level. The row's own level wins when present: round-2 tables list 专科
            # programmes run by 本科 schools.
            "education_level": record.education_level or meta.get("education_level") or school_level,
            "school_id": school_id,
            "school_code_raw": record.school_code_raw,
            "school_name_raw": record.school_name_raw,
            "major_code_raw": record.major_code_raw,
            "major_name_raw": record.major_name_raw,
            "major_name": record.major_name,
            "major_note": record.major_note,
            "program_type": extract_program_type(record.major_note),
            "selection_requirement": record.selection_requirement,
            "min_rank": record.min_rank,
            "min_rank_raw": record.min_rank_raw,
            "min_score": record.min_score,
            "plan_count": record.plan_count,
            "toudang_plan_count": record.toudang_plan_count,
            "admitted_count": record.admitted_count,
            "source_url": meta.get("notice_url"),
            "data_source": SDZK_DATA_SOURCE,
        }

    async def parse_segment_file(self, response: Response):
        if response.request is None:
            return
        meta = response.request.meta
        parse_rows = parse_spring_segment_rows if meta["kind"] == "spring_segment" else parse_segment_rows
        records = parse_rows(read_table_rows(response.body))
        if not records:
            logger.warning("No score segment rows parsed from %s (%s)", response.url, meta.get("title"))
            return
        logger.info("Parsed %d score segment rows from %s", len(records), meta.get("title"))

        province_id = await self._get_province_id()
        for record in records:
            subject_category_id = await self._resolve_subject_category(record.category or _SEGMENT_SUBJECT_CATEGORY)
            item = validate_item(
                ScoreSegmentItem,
                {
                    "province_id": province_id,
                    "year": meta["year"],
                    "subject_category_id": subject_category_id,
                    "score": record.score,
                    "segment_count": record.segment_count,
                    "cumulative_count": record.cumulative_count,
                },
            )
            if item is None:
                continue
            yield item
            await self.process_item(
                item,
                entity_type="score_segments",
                unique_keys={
                    "province_id": province_id,
                    "year": item["year"],
                    "subject_category_id": subject_category_id,
                    "score": item["score"],
                },
                upsert_fn=upsert_score_segment,
            )

    async def parse_score_line_file(self, response: Response):
        if response.request is None:
            return
        meta = response.request.meta
        text = read_pdf_text(response.body)
        if meta["kind"] == "spring_score_line":
            lines = parse_spring_score_lines(text)
        else:
            lines = parse_summer_score_lines(text, _SEGMENT_SUBJECT_CATEGORY)
        if not lines:
            logger.warning("No score lines parsed from %s (%s)", response.url, meta.get("title"))
            return
        logger.info("Parsed %d score lines from %s", len(lines), meta.get("title"))

        province_id = await self._get_province_id()
        for line in lines:
            subject_category_id = await self._resolve_subject_category(line.category)
            item = validate_item(
                ScoreLineItem,
                {
                    "province_id": province_id,
                    "year": meta["year"],
                    "subject_category_id": subject_category_id,
                    "batch": line.batch,
                    "score": line.score,
                    "special_name": line.name,
                },
            )
            if item is None:
                continue
            yield item
            await self.process_item(
                item,
                entity_type="score_lines",
                unique_keys={
                    "province_id": province_id,
                    "year": item["year"],
                    "subject_category_id": subject_category_id,
                    "batch": item["batch"],
                    "special_name": item["special_name"],
                },
                upsert_fn=upsert_score_line,
            )

    async def _get_province_id(self) -> int:
        if self._province_id is None:
            async with (await self._get_pool()).acquire() as conn:
                self._province_id = await conn.fetchval(
                    "SELECT id FROM provinces WHERE code = $1", SHANDONG_PROVINCE_CODE
                )
        if self._province_id is None:
            msg = "Province Shandong (code 37) is missing; run init-db first"
            raise RuntimeError(msg)
        return self._province_id

    async def _get_school_ids(self) -> dict[str, int]:
        if self._school_ids is None:
            async with (await self._get_pool()).acquire() as conn:
                rows = await conn.fetch("SELECT id, name, level FROM schools")
            self._school_ids = {_normalize_school_name(row["name"]): int(row["id"]) for row in rows}
            self._school_levels = {int(row["id"]): row["level"] for row in rows if row["level"]}
        return self._school_ids
