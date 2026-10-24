"""Mark private (民办), independent (独立学院) and sino-foreign (中外合作办学) schools.

Neither the school list cards nor the detail pages say who runs a school; only the list's 办学类型
filter does. Each filter is applied in one browser session and every result page is walked there,
because the filtered page URLs return an empty list when opened directly. The names are matched to
``schools`` by name (unique, and the same site wrote them).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, ClassVar

from scrapling.spiders import Request, Response

from gaokao_vault.constants import TaskType
from gaokao_vault.spiders.base import BaseGaokaoSpider
from gaokao_vault.spiders.school_spider import LIST_PAGE_SIZE, SEARCH_ENTRY_URL

logger = logging.getLogger(__name__)

# Filter label on the list page -> schools column it sets.
OWNERSHIP_FILTERS: dict[str, str] = {
    "民办高校": "is_private",
    "独立学院": "is_independent",
    "中外合作办学": "is_sino_foreign",
}
RESULT_ELEMENT_ID = "gaokao-vault-filter-result"
_MAX_PAGES = 100
_PAGE_NUMBER_PATTERN = re.compile(r"^\d+$")

# One statement per column: the column name cannot be a bind parameter.
_UPDATE_SQL: dict[str, str] = {
    column: (
        f"UPDATE schools SET {column} = (name = ANY($1::text[])) "  # noqa: S608
        f"WHERE {column} IS DISTINCT FROM (name = ANY($1::text[]))"
    )
    for column in OWNERSHIP_FILTERS.values()
}


def is_complete(names: list[str], last_page: int) -> bool:
    """Every page but the last is full, so a complete walk has more than (last_page - 1) pages of names."""
    return (last_page - 1) * LIST_PAGE_SIZE < len(names) <= last_page * LIST_PAGE_SIZE


def read_filter_result(response: Response) -> dict[str, Any] | None:
    text = response.css(f"script#{RESULT_ELEMENT_ID}::text").get("")
    if not text:
        return None
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        return None
    return result if isinstance(result, dict) else None


class _CollectFilteredNamesAction:
    """Pickle-safe page_action: apply one 办学类型 filter, walk every result page, and append the
    school names as JSON in a ``<script id=RESULT_ELEMENT_ID>`` element before the HTML is captured."""

    def __init__(self, label: str) -> None:
        self.label = label

    async def __call__(self, page):
        await page.wait_for_load_state("networkidle")
        async with page.expect_navigation(wait_until="networkidle"):
            await page.locator("span.yxtx-text", has_text=self.label).first.click()

        names: list[str] = []
        last_page = 1
        for _ in range(_MAX_PAGES):
            names += [name.strip() for name in await page.locator(".sch-item .name").all_inner_texts()]
            numbers = [
                int(text.strip())
                for text in await page.locator(".ch-page li").all_inner_texts()
                if _PAGE_NUMBER_PATTERN.match(text.strip())
            ]
            last_page = max([last_page, *numbers])
            selected = (await page.locator(".ch-page li.selected").all_inner_texts() or ["1"])[0].strip()
            next_link = page.locator(".ch-page li a").filter(has_text=re.compile(rf"^\s*{int(selected) + 1}\s*$"))
            if not _PAGE_NUMBER_PATTERN.match(selected) or await next_link.count() == 0:
                break
            async with page.expect_navigation(wait_until="networkidle"):
                await next_link.first.click()

        await page.evaluate(
            """([id, data]) => {
                const el = document.createElement('script');
                el.type = 'application/json';
                el.id = id;
                el.textContent = JSON.stringify(data);
                document.body.appendChild(el);
            }""",
            [RESULT_ELEMENT_ID, {"names": names, "last_page": last_page}],
        )
        return page


class SchoolOwnershipSpider(BaseGaokaoSpider):
    name: str = "school_ownership_spider"
    task_type: str = TaskType.SCHOOL_OWNERSHIP
    concurrent_requests: ClassVar[int] = 1

    async def start_requests(self):
        for label, column in OWNERSHIP_FILTERS.items():
            yield Request(
                SEARCH_ENTRY_URL,
                callback=self.parse,
                dont_filter=True,
                meta={"label": label, "column": column},
                page_action=_CollectFilteredNamesAction(label),
            )

    async def parse(self, response: Response):
        if response.request is None:
            return
        label = response.request.meta["label"]
        column = response.request.meta["column"]
        result = read_filter_result(response)
        if result is None:
            logger.error("No filter result captured for %s; leaving %s unchanged", label, column)
            return

        names = list(dict.fromkeys(result.get("names") or []))
        last_page = int(result.get("last_page") or 1)
        if not is_complete(names, last_page):
            logger.error(
                "Incomplete %s list (%d names over %d pages); leaving %s unchanged",
                label,
                len(names),
                last_page,
                column,
            )
            return

        async with (await self._get_pool()).acquire() as conn:
            known = {row["name"] for row in await conn.fetch("SELECT name FROM schools WHERE name = ANY($1)", names)}
            status = await conn.execute(_UPDATE_SQL[column], names)
        unmatched = [name for name in names if name not in known]
        logger.info(
            "%s: %d schools listed, %d matched, %s rows changed; unmatched=%s",
            label,
            len(names),
            len(known),
            status.split()[-1],
            unmatched[:20],
        )
        return
        yield  # make this an async generator to satisfy Request callback type
