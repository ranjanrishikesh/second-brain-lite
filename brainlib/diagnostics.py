from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal, Mapping, TypeAlias, cast


JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]


class _FrozenJSONDict(dict[str, JSONValue]):
    def _reject_mutation(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("diagnostic JSON is read-only")

    __setitem__ = _reject_mutation
    __delitem__ = _reject_mutation
    clear = _reject_mutation
    pop = _reject_mutation
    popitem = _reject_mutation
    setdefault = _reject_mutation
    update = _reject_mutation
    __ior__ = _reject_mutation


class _FrozenJSONList(list[JSONValue]):
    def _reject_mutation(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("diagnostic JSON is read-only")

    __setitem__ = _reject_mutation
    __delitem__ = _reject_mutation
    append = _reject_mutation
    clear = _reject_mutation
    extend = _reject_mutation
    insert = _reject_mutation
    pop = _reject_mutation
    remove = _reject_mutation
    reverse = _reject_mutation
    sort = _reject_mutation
    __iadd__ = _reject_mutation
    __imul__ = _reject_mutation


def freeze_json_mapping(
    value: Mapping[str, JSONValue],
) -> Mapping[str, JSONValue]:
    return cast(Mapping[str, JSONValue], _freeze_json(value))


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return _FrozenJSONDict(
            (cast(str, key), cast(JSONValue, _freeze_json(item)))
            for key, item in value.items()
        )
    if isinstance(value, list):
        return _FrozenJSONList(cast(JSONValue, _freeze_json(item)) for item in value)
    return value


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    path: PurePosixPath | None = None
    details: Mapping[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", freeze_json_mapping(self.details))


@dataclass(frozen=True)
class ValidationIssue:
    severity: Literal["error", "warning"]
    code: str
    message: str
    path: PurePosixPath | None = None
    details: Mapping[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", freeze_json_mapping(self.details))


@dataclass(frozen=True)
class ValidationReport:
    checks: tuple[str, ...]
    issues: tuple[ValidationIssue, ...]
    corpus_revision: str | None

    @property
    def ok(self) -> bool:
        return all(issue.severity != "error" for issue in self.issues)
