"""Shared request dependencies and form helpers."""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import time
from typing import Annotated
from urllib.parse import urlencode

from fastapi import Depends, Request
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from repeater.db import get_sessionmaker


def get_db() -> Iterator[Session]:
    """One SQLAlchemy session per request. Routes commit explicitly; close() rolls back
    anything left uncommitted so no connection stays idle in a transaction."""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


async def get_form(request: Request) -> FormData:
    """The parsed url-encoded form. Already parsed (and cached) by the CSRF check."""
    return await request.form(max_files=0)


DB = Annotated[Session, Depends(get_db)]
Form = Annotated[FormData, Depends(get_form)]


class FormError(ValueError):
    """A user input problem. `key` is an i18n key, `params` its placeholders."""

    def __init__(self, key: str, **params: object) -> None:
        super().__init__(key)
        self.key = key
        self.params = params


def form_str(form: FormData, name: str, *, max_len: int = 500, required: bool = False) -> str:
    raw = form.get(name)
    value = raw.strip() if isinstance(raw, str) else ""
    if required and not value:
        raise FormError("error.field_required", field=name)
    if len(value) > max_len:
        raise FormError("error.field_too_long", field=name, max=max_len)
    return value


def form_text(form: FormData, name: str, *, max_len: int = 5000) -> str:
    """Multi-line text: keeps inner whitespace, normalises newlines."""
    raw = form.get(name)
    value = raw.replace("\r\n", "\n").strip() if isinstance(raw, str) else ""
    if len(value) > max_len:
        raise FormError("error.field_too_long", field=name, max=max_len)
    return value


def form_bool(form: FormData, name: str) -> bool:
    return form.get(name) in ("1", "on", "true", "yes")


def form_int(form: FormData, name: str, *, minimum: int, maximum: int, default: int | None = None) -> int:
    raw = form.get(name)
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        if default is not None:
            return default
        raise FormError("error.field_required", field=name)
    try:
        value = int(text)
    except ValueError:
        raise FormError("error.not_a_number", field=name) from None
    if not minimum <= value <= maximum:
        raise FormError("error.out_of_range", field=name, min=minimum, max=maximum)
    return value


def form_time(form: FormData, name: str) -> time:
    raw = form.get(name)
    text = raw.strip() if isinstance(raw, str) else ""
    try:
        hours, minutes = text.split(":")[:2]
        return time(int(hours), int(minutes))
    except (ValueError, TypeError):
        raise FormError("error.bad_time", field=name) from None


def query_int(value: str | None, *, default: int, minimum: int = 1, maximum: int = 100_000) -> int:
    try:
        return max(minimum, min(int(value or default), maximum))
    except ValueError:
        return default


@dataclass(frozen=True)
class Page:
    number: int
    size: int
    total: int

    @property
    def offset(self) -> int:
        return (self.number - 1) * self.size

    @property
    def pages(self) -> int:
        return max(1, math.ceil(self.total / self.size))

    @property
    def has_prev(self) -> bool:
        return self.number > 1

    @property
    def has_next(self) -> bool:
        return self.number < self.pages

    def link(self, path: str, number: int, **params: str | None) -> str:
        query = {k: v for k, v in params.items() if v}
        query["page"] = str(number)
        return f"{path}?{urlencode(query)}"


def safe_next(target: str | None, default: str = "/") -> str:
    """Only allow local absolute paths as redirect targets (no scheme, host or `//`)."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return default
    if any(ch in target for ch in "\r\n\t"):
        return default
    return target
