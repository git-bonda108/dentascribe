"""Smoke tests. Each batch adds more.

Batch 1: only checks the repo skeleton exists.
"""
from __future__ import annotations
import pathlib

ROOT = pathlib.Path(__file__).parent.parent


def test_repo_skeleton_exists():
    expected = [
        "README.md",
        "pyproject.toml",
        ".env.example",
        ".gitignore",
        "core",
        "prompts",
        "agents",
        "streaming",
        "audio_pipeline",
        "exports",
        "ui/components",
        "data/samples",
        "tests",
        "docs",
    ]
    for rel in expected:
        assert (ROOT / rel).exists(), f"Missing: {rel}"


def test_readme_has_anchors():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for anchor in [
        "DentaScribe",
        "TSBDE",
        "claude-sonnet-4-5",
        "uv",
        "docs/ARCHITECTURE.md",
    ]:
        assert anchor in text, f"README.md missing anchor: {anchor}"
