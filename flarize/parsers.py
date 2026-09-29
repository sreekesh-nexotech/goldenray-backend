"""Request body parsers (``REST_FRAMEWORK["DEFAULT_PARSER_CLASSES"]``).

:class:`JSONParser` is DRF's parser with one difference: a body nested deeper than the interpreter's recursion limit
(``[[[[…]]]]``, a few hundred kilobytes) made ``json.load`` raise ``RecursionError``, which DRF does not treat as a
parse failure — every JSON endpoint answered 500 and recorded a ``SystemException`` for an anonymous client's
malformed body. It is a malformed body: ``400 parse_error``, like any other JSON that cannot be read.
"""

from __future__ import annotations

from rest_framework import parsers
from rest_framework.exceptions import ParseError


class JSONParser(parsers.JSONParser):
    def parse(self, stream, media_type=None, parser_context=None):
        try:
            return super().parse(stream, media_type=media_type, parser_context=parser_context)
        except RecursionError:
            raise ParseError("JSON parse error - the body is nested too deeply.") from None
