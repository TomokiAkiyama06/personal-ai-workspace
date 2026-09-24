"""Research / Knowledge Layer of the Core Backend.

``providers`` (PAW-051) holds the provider-neutral adapter interface and the
fan-out that hides which provider produced what. ``scratch`` is the Research
Scratch Store (PAW-050): temporary, 24 hour, project-scoped research results
that are kept apart from Long-term Memory. See ``apps/backend/README.md``.
"""
