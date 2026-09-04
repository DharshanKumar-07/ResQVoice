"""Local SOP retrieval using deterministic embedding search over Markdown runbooks."""
from __future__ import annotations

import hashlib
import math
import re
from functools import lru_cache
from pathlib import Path

from app.schemas import SOPMatch


SOP_DIRECTORY = Path(__file__).resolve().parents[3] / "docs" / "sops"
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STEP_RE = re.compile(r"^\s*\d+\.\s+(.+?)\s*$")
_EMBEDDING_DIMENSIONS = 384

# Lightweight semantic expansion keeps retrieval local and deterministic while
# still matching operational equivalents rather than requiring exact phrases.
_SEMANTIC_ALIASES = {
    "rollback": ("revert", "deployment", "release", "known-good"),
    "revert": ("rollback", "deployment", "release"),
    "shutdown": ("stop", "drain", "system", "server"),
    "shut": ("shutdown", "stop", "drain", "system", "server"),
    "restart": ("service", "production", "change", "health"),
    "config": ("configuration", "production", "change"),
    "configuration": ("config", "production", "change"),
    "scale": ("capacity", "infrastructure", "production", "change"),
    "scaling": ("scale", "capacity", "infrastructure"),
    "compromise": ("security", "database", "forensic", "containment"),
    "breach": ("security", "compromise", "containment"),
    "communication": ("external", "incident", "message", "stakeholder"),
    "communicate": ("communication", "external", "incident", "stakeholder"),
    "escalation": ("escalate", "incident", "security", "commander"),
}


def _tokens(text: str) -> list[str]:
    tokens = _TOKEN_RE.findall(text.lower())
    expanded = list(tokens)
    for token in tokens:
        expanded.extend(_SEMANTIC_ALIASES.get(token, ()))
    return expanded


def embed_text(text: str) -> tuple[float, ...]:
    """Return a normalized feature-hash embedding suitable for cosine search."""
    vector = [0.0] * _EMBEDDING_DIMENSIONS
    for token in _tokens(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % _EMBEDDING_DIMENSIONS
        vector[index] += 1.0
    magnitude = math.sqrt(sum(value * value for value in vector))
    if magnitude:
        vector = [value / magnitude for value in vector]
    return tuple(vector)


def embedding_similarity(left: str, right: str) -> float:
    return sum(a * b for a, b in zip(embed_text(left), embed_text(right)))


@lru_cache(maxsize=1)
def _load_sops() -> tuple[tuple[str, str, str, tuple[str, ...]], ...]:
    documents = []
    for path in sorted(SOP_DIRECTORY.glob("*.md")):
        content = path.read_text(encoding="utf-8")
        title_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else path.stem
        steps = tuple(
            match.group(1).strip()
            for line in content.splitlines()
            if (match := _STEP_RE.match(line))
        )
        if not steps:
            raise ValueError(f"SOP '{path}' has no numbered required steps.")
        reference = path.relative_to(SOP_DIRECTORY.parents[1]).as_posix()
        documents.append((title, reference, content, steps))
    if not documents:
        raise RuntimeError(f"No SOP Markdown files found in {SOP_DIRECTORY}")
    return tuple(documents)


def retrieve_relevant_sop(proposed_action: str) -> SOPMatch:
    """Retrieve the closest SOP by cosine similarity over local embeddings."""
    action = proposed_action.strip()
    if not action:
        raise ValueError("proposed_action must not be empty.")

    ranked = [
        (embedding_similarity(action, content), title, reference, steps)
        for title, reference, content, steps in _load_sops()
    ]
    score, title, reference, steps = max(ranked, key=lambda item: item[0])
    return SOPMatch(
        title=title,
        reference=reference,
        score=round(score, 4),
        required_steps=list(steps),
    )
