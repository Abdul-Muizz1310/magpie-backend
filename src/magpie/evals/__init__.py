"""Reproducible evals for magpie.

`heal_rate` measures how many selector-breakage archetypes the shipped heal loop
repairs. `offline_proposer` supplies the one stage that must not call an LLM for
the number to reproduce. See ``docs/specs/07-heal-rate-eval.md``.
"""
