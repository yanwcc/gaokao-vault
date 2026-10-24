from __future__ import annotations

import asyncio
import gzip
from unittest.mock import AsyncMock, MagicMock, patch

from gaokao_vault.config import CrawlConfig, DatabaseConfig
from gaokao_vault.spiders.base import BaseGaokaoSpider
from gaokao_vault.storage.raw_archive import RawArchive, guess_extension


class _Acquire:
    async def __aenter__(self):
        return MagicMock()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def acquire(self):
        return _Acquire()


def _make_response(body: bytes, url: str, status: int = 200) -> MagicMock:
    response = MagicMock()
    response.status = status
    response.url = url
    response.body = body
    response.headers = {"Content-Type": "text/html; charset=utf-8"}
    response.request = MagicMock()
    response.request.url = url
    response.request.meta = {"sch_id": 7, "allowed_domains": frozenset({"x"})}
    response.request._session_kwargs = {}
    return response


def test_save_is_content_addressed_and_round_trips(tmp_path):
    archive = RawArchive(tmp_path)

    first = asyncio.run(archive.save(b"<html>a</html>", url="https://gaokao.chsi.com.cn/sch/x.dhtml"))
    second = asyncio.run(archive.save(b"<html>a</html>", url="https://gaokao.chsi.com.cn/sch/y.dhtml"))

    assert first == second
    content_hash, key = first
    assert key == f"gaokao.chsi.com.cn/{content_hash[:2]}/{content_hash}.bin.gz"
    assert gzip.decompress((tmp_path / key).read_bytes()) == b"<html>a</html>"
    assert archive.load(key) == b"<html>a</html>"


def test_guess_extension_prefers_url_suffix_then_content_type():
    assert guess_extension("https://www.sdzk.cn/Floadup/file/a.XLS", "application/octet-stream") == ".xls"
    assert guess_extension("https://www.sdzk.cn/NewsInfo.aspx?NewsID=1", "text/html; charset=utf-8") == ".html"
    assert guess_extension("https://static-data.gaokao.cn/x", "application/json") == ".json"
    assert guess_extension("https://example.com/x", None) == ".bin"


def _spider(tmp_path, *, archive_raw: bool = True) -> BaseGaokaoSpider:
    config = CrawlConfig(raw_dir=str(tmp_path), archive_raw=archive_raw)
    return BaseGaokaoSpider(
        db_config=DatabaseConfig(dsn="postgresql://t:t@localhost/t"), crawl_task_id=3, config=config
    )


def test_unblocked_response_is_archived_and_indexed(tmp_path):
    spider = _spider(tmp_path)
    response = _make_response(b"<html>school</html>", "https://gaokao.chsi.com.cn/sch/schoolInfoMain--schId-1.dhtml")

    with (
        patch.object(spider, "_get_pool", new=AsyncMock(return_value=_FakePool())),
        patch("gaokao_vault.db.queries.sources.upsert_data_source", new=AsyncMock(return_value=11)) as upsert_source,
        patch("gaokao_vault.db.queries.sources.upsert_source_document", new=AsyncMock(return_value=1)) as upsert_doc,
    ):
        blocked = asyncio.run(spider.is_blocked(response))

    assert blocked is False
    assert upsert_source.await_args is not None
    assert upsert_doc.await_args is not None
    assert upsert_source.await_args.args[1]["authority_level"] == 100
    document = upsert_doc.await_args.args[1]
    assert document["data_source_id"] == 11
    assert document["storage_key"].endswith(".html.gz")
    assert document["metadata"]["meta"] == {"sch_id": 7}
    assert document["metadata"]["crawl_task_id"] == 3
    assert RawArchive(tmp_path).load(document["storage_key"]) == b"<html>school</html>"


def test_blocked_response_is_not_archived(tmp_path):
    spider = _spider(tmp_path)
    response = _make_response("<html>访问过于频繁</html>".encode(), "https://gaokao.chsi.com.cn/sch/x.dhtml")

    with patch.object(spider, "_archive_response", new=AsyncMock()) as archive:
        assert asyncio.run(spider.is_blocked(response)) is True

    archive.assert_not_awaited()


def test_archiving_is_off_without_config_or_when_disabled(tmp_path):
    no_config = BaseGaokaoSpider(db_config=DatabaseConfig(dsn="postgresql://t:t@localhost/t"), crawl_task_id=1)
    disabled = _spider(tmp_path, archive_raw=False)
    response = _make_response(b"<html>ok</html>", "https://gaokao.chsi.com.cn/sch/x.dhtml")

    for spider in (no_config, disabled):
        with patch.object(spider, "_get_pool", new=AsyncMock()) as get_pool:
            assert asyncio.run(spider.is_blocked(response)) is False
        get_pool.assert_not_awaited()
    assert not any(tmp_path.iterdir())
