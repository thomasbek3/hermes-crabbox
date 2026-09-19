"""Shared evidence guidance; browser execution remains in the existing terminal."""
from pathlib import Path


def register(ctx):
    root = Path(__file__).parent / 'skills'
    for name in ('pr-evidence', 'agent-browser'):
        ctx.register_skill(name, root / name / 'SKILL.md')
