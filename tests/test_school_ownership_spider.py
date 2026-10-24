from __future__ import annotations

import inspect
import json
import pickle
from unittest.mock import MagicMock

from scrapling.parser import Adaptor

from gaokao_vault.db.queries.schools import upsert_school
from gaokao_vault.spiders.school_ownership_spider import (
    OWNERSHIP_FILTERS,
    RESULT_ELEMENT_ID,
    _CollectFilteredNamesAction,
    is_complete,
    read_filter_result,
)


def _response(html: str) -> MagicMock:
    response = MagicMock()
    response.css = Adaptor(content=html).css
    return response


def _response_with(payload: str) -> MagicMock:
    return _response(
        f'<html><body><script type="application/json" id="{RESULT_ELEMENT_ID}">{payload}</script></body></html>'
    )


def test_filters_cover_the_three_ownership_columns():
    assert OWNERSHIP_FILTERS == {
        "民办高校": "is_private",
        "独立学院": "is_independent",
        "中外合作办学": "is_sino_foreign",
    }


def test_is_complete_requires_every_page_but_the_last_to_be_full():
    assert is_complete(["x"] * 690, 35)
    assert is_complete(["x"] * 12, 1)
    assert not is_complete(["x"] * 20, 35)
    assert not is_complete([], 1)
    assert not is_complete(["x"] * 701, 35)


def test_read_filter_result_parses_the_injected_json():
    payload = json.dumps({"names": ["北京城市学院", "天津天狮学院"], "last_page": 35}, ensure_ascii=False)

    assert read_filter_result(_response_with(payload)) == {"names": ["北京城市学院", "天津天狮学院"], "last_page": 35}
    assert read_filter_result(_response_with("not json")) is None
    assert read_filter_result(_response("<html><body></body></html>")) is None


def test_collect_action_is_pickle_safe():
    # Round-trips an object built here; page actions are pickled when requests are queued.
    action = pickle.loads(pickle.dumps(_CollectFilteredNamesAction("民办高校")))  # noqa: S301

    assert action.label == "民办高校"


def test_school_upsert_leaves_ownership_flags_to_the_ownership_task():
    sql = inspect.getsource(upsert_school)
    update_clause = sql.split("DO UPDATE SET", 1)[1]

    for column in OWNERSHIP_FILTERS.values():
        assert f"{column}=EXCLUDED" not in update_clause
