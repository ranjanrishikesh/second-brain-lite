from __future__ import annotations

import importlib.util
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .registry import (
    ExtractorRegistry,
    RunVersionProbe,
    detect_converter_version,
)

_DISTRIBUTION_MODULES = {
    "PyMuPDF": ("pymupdf", "fitz"),
    "python-docx": ("docx",),
    "python-pptx": ("pptx",),
    "openpyxl": ("openpyxl",),
}


@dataclass(frozen=True)
class Capability:
    converter_id: str
    available: bool
    detected_version: str | None
    detail: str
    install_recipes: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "install_recipes",
            MappingProxyType(
                {key: tuple(value) for key, value in self.install_recipes.items()}
            ),
        )


def probe_capabilities(
    registry: ExtractorRegistry,
    *,
    run: RunVersionProbe,
) -> Mapping[str, Capability]:
    """Inspect prerequisites without importing converters or running extraction."""

    capabilities: dict[str, Capability] = {}
    for extractor in registry.extractors:
        for converter in (extractor.preferred, *extractor.fallbacks):
            if converter.converter_id in capabilities:
                continue
            version = None
            try:
                if converter.executable is not None:
                    if shutil.which(converter.executable) is None:
                        raise OSError("Executable is not installed.")
                if converter.python_distribution is not None:
                    distribution = converter.python_distribution
                    modules = _DISTRIBUTION_MODULES.get(
                        distribution,
                        (distribution.replace("-", "_"),),
                    )
                    if not any(
                        importlib.util.find_spec(module) is not None
                        for module in modules
                    ):
                        raise ImportError("Python module is not installed.")
                version = detect_converter_version(
                    converter,
                    extractor_version=extractor.extractor_version,
                    run=run,
                )
                detail = "Available."
            except (
                OSError,
                ImportError,
                RuntimeError,
                ValueError,
                subprocess.SubprocessError,
            ) as error:
                detail = str(error) or "Version probe failed."
            capabilities[converter.converter_id] = Capability(
                converter.converter_id,
                version is not None,
                version,
                detail,
                converter.install_recipes,
            )
    return MappingProxyType(capabilities)
