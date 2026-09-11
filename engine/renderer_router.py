from .core import FilmError


def route(shot, available, quality="final"):
    """Policy preference, not a benchmark claim. Availability always wins."""
    explicit = shot.get("renderer", "auto")
    if explicit not in {"auto", "mock", "manual", "openart"}:
        if explicit not in available:
            raise FilmError(f"Renderer {explicit} has no verified capability/price configuration")
        return explicit
    preferences = []
    if shot.get("task") == "repair":
        preferences += ["wan3-0"]
    if shot.get("identity_priority") == "very_high":
        preferences += ["kling-3-omni"]
    if shot["duration_ms"] > 15000:
        preferences += ["byte-plus-seedance-2-5", "wan3-0"]
    if quality == "draft" or shot["motion"].get("complexity") == "low":
        preferences += ["byte-plus-seedance-2-fast", "fal-h3-max"]
    preferences += ["byte-plus-seedance-2-5", "kling-3-omni", "byte-plus-seedance-2-fast"]
    for name in preferences:
        if name in available:
            return name
    raise FilmError("No configured renderer supports this shot; refresh provider forms and quotes")
