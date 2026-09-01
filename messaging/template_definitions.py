"""Hardcoded email template definitions and per-template context dataclasses."""

from dataclasses import dataclass, fields
from datetime import date, datetime
from types import MappingProxyType


def preview_defaults(defaults: dict[str, object]):
    """Attach preview-only default context values to a template dataclass."""

    frozen_defaults = MappingProxyType(dict(defaults))

    def _decorator(cls):
        dataclass_fields = getattr(cls, "__dataclass_fields__", {})
        unknown_fields = sorted(set(frozen_defaults.keys()) - set(dataclass_fields.keys()))
        if unknown_fields:
            raise ValueError(f"Unknown preview default field(s) for {cls.__name__}: {', '.join(unknown_fields)}")
        cls.__preview_defaults__ = frozen_defaults
        return cls

    return _decorator


@dataclass(frozen=True, slots=True)
class TemplateDefinition:
    key: str
    name: str
    context_dataclass: type


_TEMPLATE_DEFINITIONS = MappingProxyType({})


def get_template_definition(template_key: str) -> TemplateDefinition | None:
    if not template_key:
        return None
    return _TEMPLATE_DEFINITIONS.get(template_key)


def get_allowed_template_keys() -> list[str]:
    return sorted(_TEMPLATE_DEFINITIONS.keys())


def _serialize_preview_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _preview_defaults_for_dataclass(context_dataclass: type) -> dict[str, object]:
    dataclass_field_names = {dataclass_field.name for dataclass_field in fields(context_dataclass)}
    declared_defaults = getattr(context_dataclass, "__preview_defaults__", {})
    defaults: dict[str, object] = {}
    for field_name, field_value in declared_defaults.items():
        if field_name not in dataclass_field_names:
            continue
        value = _serialize_preview_default(field_value)
        if value is not None:
            defaults[field_name] = value
    return defaults


def get_context_default_values(template_key: str) -> dict[str, object]:
    definition = get_template_definition(template_key)
    if definition is None:
        return {}
    return _preview_defaults_for_dataclass(definition.context_dataclass)


def get_context_variable_names(template_key: str) -> list[str]:
    definition = get_template_definition(template_key)
    if definition is None:
        return []
    return [field.name for field in fields(definition.context_dataclass)]
