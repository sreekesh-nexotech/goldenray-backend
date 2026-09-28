"""Pagination classes. No endpoint returns an unbounded list.

* :class:`StandardPagination` — page-number, ``page_size`` 25 by default, client may ask for up to 200.
  Response: ``{results, count, next, previous}``.
* :class:`CreatedAtCursorPagination` — cursor pagination for append-only logs (stable under concurrent inserts).
  Response: ``{results, next, previous}``. Subclass and set ``ordering`` for tables ordered by another column.
"""

from collections import OrderedDict

from rest_framework.pagination import CursorPagination, PageNumberPagination
from rest_framework.response import Response


class StandardPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 200

    def get_paginated_response(self, data):
        return Response(
            OrderedDict(
                [
                    ("results", data),
                    ("count", self.page.paginator.count),
                    ("next", self.get_next_link()),
                    ("previous", self.get_previous_link()),
                ]
            )
        )

    def get_paginated_response_schema(self, schema):
        return {
            "type": "object",
            "required": ["results", "count", "next", "previous"],
            "properties": {
                "results": schema,
                "count": {"type": "integer", "example": 123},
                "next": {"type": "string", "nullable": True, "format": "uri"},
                "previous": {"type": "string", "nullable": True, "format": "uri"},
            },
        }


class CreatedAtCursorPagination(CursorPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200
    ordering = ("-created_at", "-id")

    def get_paginated_response(self, data):
        return Response(OrderedDict([("results", data), ("next", self.get_next_link()), ("previous", self.get_previous_link())]))

    def get_paginated_response_schema(self, schema):
        return {
            "type": "object",
            "required": ["results", "next", "previous"],
            "properties": {
                "results": schema,
                "next": {"type": "string", "nullable": True, "format": "uri"},
                "previous": {"type": "string", "nullable": True, "format": "uri"},
            },
        }
