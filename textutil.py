"""Utilidades de texto compartidas por el scraper y el filtro/ranking."""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path


def normalize(text: str | None) -> str:
    """Minúsculas y sin acentos, para comparar español/inglés sin sorpresas."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(text))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


@lru_cache(maxsize=None)
def _keyword_pattern(keyword: str) -> re.Pattern:
    """Regex de una keyword: subcadena sin distinguir mayúsculas, en límite de palabra.

    El límite de palabra evita falsos positivos de keywords cortas
    ("AWS" dentro de "laws", "Linux" dentro de otra palabra). Espacios y
    guiones son intercambiables ("low-code" == "low code").
    """
    parts = [re.escape(p) for p in re.split(r"[\s\-]+", normalize(keyword)) if p]
    return re.compile(r"(?<![a-z0-9])" + r"[\s\-]+".join(parts) + r"(?![a-z0-9])")


def keyword_in(keyword: str, normalized_text: str) -> bool:
    """True si ``keyword`` aparece en un texto ya pasado por :func:`normalize`."""
    return bool(normalized_text) and bool(_keyword_pattern(keyword).search(normalized_text))


def load_keywords(path: str | Path = "keywords.json") -> dict:
    """Carga keywords.json (aristas, filtros y objetivo salarial)."""
    return json.loads(Path(path).read_text(encoding="utf-8"))
