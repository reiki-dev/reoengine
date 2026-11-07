"""Explicit skill selection; token counts are estimates (len(text) / 4)."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .config import Profile


@dataclass(frozen=True)
class Skill:
    name: str
    text: str


def select_skills(profile: Profile, catalog: Mapping[str, str]) -> tuple[Skill, ...]:
    if len(profile.skills) > min(profile.max_skills, 8):
        raise ValueError("skill_count_exceeded")
    if len(set(profile.skills)) != len(profile.skills):
        raise ValueError("Duplicate skills")
    selected = tuple(Skill(name, catalog[name]) for name in profile.skills if name in catalog)
    if len(selected) != len(profile.skills):
        raise ValueError("Missing skill text")
    if sum(len(skill.text) for skill in selected) / 4 > profile.max_skill_tokens:
        raise ValueError("skill_tokens_exceeded (estimate: len(text)/4)")
    return selected
