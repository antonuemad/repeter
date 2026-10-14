"""Language-independent domain messages with an English diagnostic fallback.

Keys and parameters travel with activity/error records. English remains useful to operators;
Arabic is rendered from the key, never inferred from the wording of an exception.
"""

from __future__ import annotations

from collections.abc import Callable
from string import Formatter
from typing import Any

from repeater.message_catalog import AR


class Message(str):
    key: str
    params: dict[str, Any]

    def __new__(cls, key: str, fallback: str, params: dict[str, Any] | None = None):
        instance = super().__new__(cls, fallback)
        values = dict(params or {})
        if key in AR:
            fields = {field for _, field, _, _ in Formatter().parse(AR[key]) if field}
            values = {name: value for name, value in values.items() if name in fields}
        instance.key, instance.params = key, values
        return instance

    def __reduce__(self):
        return (Message, (self.key, str(self), self.params))

    @property
    def info(self) -> dict[str, Any]:
        return {"key": self.key, "params": _pack(self.params), "fallback": str(self)}

    def __getitem__(self, item):
        return Message(self.key, super().__getitem__(item), self.params)

    def strip(self, chars=None):
        return Message(self.key, super().strip(chars), self.params)

    def remap(self, transform: Callable[[str], str]) -> Message:
        """Scrub both the diagnostic and every persisted parameter (URLs may be credentials)."""

        def clean(value):
            if isinstance(value, Message):
                return value.remap(transform)
            if isinstance(value, str):
                return transform(value)
            if isinstance(value, dict):
                return {k: clean(v) for k, v in value.items()}
            if isinstance(value, list):
                return [clean(v) for v in value]
            return value

        return Message(self.key, transform(str(self)), clean(self.params))


def _pack(value):
    if isinstance(value, Message):
        return {"$message": value.info}
    if isinstance(value, dict):
        return {k: _pack(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_pack(v) for v in value]
    if isinstance(value, str):
        return value[:4000]
    return value


def msg(key: str, english: str, **params: Any) -> Message:
    return Message(key, english.format_map(params), params)


def diagnostic(text: str, key: str = "error.generic", **params: Any) -> Message:
    return text if isinstance(text, Message) else Message(key, str(text), params)


def render(info: dict, lang: str, fallback: str = "") -> str:
    english = info.get("fallback", fallback)
    if lang != "ar" or info.get("key") not in AR:
        return english

    def translate(value):
        if isinstance(value, dict) and "$message" in value:
            return render(value["$message"], lang)
        if isinstance(value, list):
            return "، ".join(str(translate(v)) for v in value)
        return value

    params = {k: translate(v) for k, v in (info.get("params") or {}).items()}
    try:
        return AR[info["key"]].format_map(params)
    except (KeyError, ValueError):
        return english  # Old or malformed records must remain readable, never break the page.


class UserError(ValueError):
    """An action refusal whose translation does not depend on the English sentence."""

    def __init__(self, key: str, english: str, **params: Any):
        super().__init__(english.format_map(params))
        self.key, self.params = key, params
