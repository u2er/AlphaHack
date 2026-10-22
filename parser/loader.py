"""Load an alternative parser from the parser package."""
from __future__ import annotations

from importlib import import_module


def load_parser(spec: str):
    module_name, separator, function_name = spec.partition(':')
    if not separator or not module_name.startswith('parser.') or not function_name.isidentifier():
        raise ValueError('Parser must be parser.module:function')
    function = getattr(import_module(module_name), function_name)
    if not callable(function):
        raise ValueError(f'Parser is not callable: {spec}')
    return function
