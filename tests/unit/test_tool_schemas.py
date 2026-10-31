#!/usr/bin/env python3
"""
What the tool list costs: it goes to the model on every request

Licensed under the MIT License - see LICENSE file for details
"""

import test_imports  # Automatic path setup
from server.mcp_tools import FAILURE_RESULT, PageContent, slim_schema


def test_an_optional_is_just_its_type():
    schema = {'title': 'Args', 'type': 'object', 'additionalProperties': False, 'properties': {
        'ref': {'anyOf': [{'type': 'string'}, {'type': 'null'}], 'default': None, 'title': 'Ref', 'description': 'A ref'},
    }}
    assert slim_schema(schema) == {'type': 'object', 'properties': {'ref': {'type': 'string', 'description': 'A ref'}}}


def test_a_property_called_title_stays():
    schema = {'type': 'object', 'properties': {'title': {'title': 'Title', 'type': 'string'}}}
    assert slim_schema(schema) == {'type': 'object', 'properties': {'title': {'type': 'string'}}}


def test_null_stays_where_it_means_something():
    schema = {'anyOf': [{'type': 'integer'}, {'type': 'null'}], 'default': 20000}
    assert slim_schema(schema) == schema


def test_a_union_of_two_real_types_stays_and_a_real_default_stays():
    schema = {'anyOf': [{'type': 'integer'}, {'type': 'string'}, {'type': 'null'}], 'default': 5}
    assert slim_schema(schema) == {'anyOf': [{'type': 'integer'}, {'type': 'string'}, {'type': 'null'}], 'default': 5}


def test_page_text_that_starts_like_a_failure_is_not_one():
    assert not FAILURE_RESULT.match("x")
    assert isinstance(PageContent("Error 404 Not Found"), str)
