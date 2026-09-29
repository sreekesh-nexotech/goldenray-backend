"""``{% quotation_artwork payload language as d %}`` — the artwork's view of a frozen document (formatting only)."""

from django import template

from quotations.services.artwork import view_model

register = template.Library()


@register.simple_tag
def quotation_artwork(document, language):
    return view_model(document or {}, language)
