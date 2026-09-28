import pytest
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from core.models import OutboxEvent
from flarize.pagination import CreatedAtCursorPagination, StandardPagination

factory = APIRequestFactory()


def _paginate(query: str, items=None):
    request = Request(factory.get(f"/api/v1/things/{query}"))
    paginator = StandardPagination()
    page = paginator.paginate_queryset(items if items is not None else list(range(500)), request)
    return paginator, page


def test_default_page_size_is_25():
    paginator, page = _paginate("")
    assert page == list(range(25))
    response = paginator.get_paginated_response(page)
    assert list(response.data) == ["results", "count", "next", "previous"]
    assert response.data["count"] == 500
    assert response.data["previous"] is None
    assert "page=2" in response.data["next"]


@pytest.mark.parametrize(("query", "expected"), [("?page_size=50", 50), ("?page_size=200", 200), ("?page_size=1000", 200), ("?page_size=0", 25), ("?page_size=-5", 25), ("?page_size=abc", 25)])
def test_page_size_bounds(query, expected):
    _, page = _paginate(query)
    assert len(page) == expected


def test_last_page_and_out_of_range():
    paginator, page = _paginate("?page=20")
    assert page == list(range(475, 500))
    assert paginator.get_paginated_response(page).data["next"] is None
    with pytest.raises(NotFound):
        _paginate("?page=21")
    with pytest.raises(NotFound):
        _paginate("?page=abc")


def test_schema_lists_the_envelope():
    schema = StandardPagination().get_paginated_response_schema({"type": "array"})
    assert schema["required"] == ["results", "count", "next", "previous"]


@pytest.mark.django_db
def test_cursor_pagination_walks_every_row_once():
    for index in range(7):
        OutboxEvent.objects.create(event_type="tests.cursor", payload={"i": index})
    seen = []
    url = "/api/v1/log/?page_size=3"
    for _ in range(5):
        request = Request(factory.get(url))
        paginator = CreatedAtCursorPagination()
        page = paginator.paginate_queryset(OutboxEvent.objects.all(), request)
        seen.extend(row.payload["i"] for row in page)
        data = paginator.get_paginated_response([row.id for row in page]).data
        assert list(data) == ["results", "next", "previous"]
        if not data["next"]:
            break
        url = data["next"]
    assert sorted(seen) == list(range(7))
    assert len(seen) == 7
