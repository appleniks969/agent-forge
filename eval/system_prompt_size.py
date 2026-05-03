#!/usr/bin/env python3
"""
system_prompt_size.py — measure agent-forge's system prompt size offline.

Imports build_system_prompt() and counts characters + estimated tokens for
a given working directory. Useful for the "is the prompt heavier post-refactor?"
hypothesis check — does not require an LLM call.

Usage:
    system_prompt_size.py --cwd /tmp/run-forge-1
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cwd", required=True, type=Path)
    args = ap.parse_args()

    # Late imports so this script is harmless even if agent_forge is broken.
    try:
        from agent_forge.chat import ChatConfig
        from agent_forge.prompts import build_chat_prompt
        from agent_forge.tools import default_registry
    except Exception as e:
        json.dump({"error": f"import failed: {e}"}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 1

    # Char-count // 4 ≈ tokens (matches agent_forge's internal heuristic).
    def estimate_tokens(text: str) -> int:
        return len(text) // 4

    registry = default_registry()
    cfg = ChatConfig(api_key="dummy", cwd=str(args.cwd))
    sp = build_chat_prompt(cfg, registry)
    built = sp.build()

    # SystemPromptSection only carries .text + .cache_control; section
    # provenance lives in the registry order. Walk in registration order so
    # we can label each chunk by the SectionName it came from.
    section_names = [name.name for name in sp._sections.keys()]  # type: ignore[attr-defined]

    per_section = []
    total_chars = 0
    total_tokens = 0
    for idx, section in enumerate(built):
        chars = len(section.text)
        toks = estimate_tokens(section.text)
        total_chars += chars
        total_tokens += toks
        label = section_names[idx] if idx < len(section_names) else f"section_{idx}"
        per_section.append(
            {
                "name": label,
                "cache_control": section.cache_control,
                "chars": chars,
                "estimated_tokens": toks,
            }
        )

    json.dump(
        {
            "cwd": str(args.cwd),
            "section_count": len(built),
            "total_chars": total_chars,
            "estimated_total_tokens": total_tokens,
            "sections": per_section,
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
