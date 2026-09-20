"""Part 14 AI adapter (+ addendum C). Geometry measures, AI adjudicates,
humans confirm.

Two narrow methods with constrained output spaces:
- group_text_lines: partition of supplied line indices, nothing else.
- attribute_dimension: ranking of supplied region ids, nothing else.

Both validate on receipt and fall back to the geometric result on any
malformed output. With ai.enabled: false (the default) neither is ever
called — the system loses recall on messy drawings, never correctness.
Only "disabled" ships in this repo; firm backends subclass Adapter and
register via config/ai.yaml backend name.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


class AIDisabled(Exception):
    """Raised when an AI method is called while ai.enabled is false."""


class AIInvalidOutput(Exception):
    """Raised when adapter output fails receipt validation (caller falls back)."""


@dataclass
class TextLine:
    index: int
    text: str
    bbox: list[float]  # [x0, y0, x1, y1] model mm
    height: float | None = None
    rotation: float | None = None


@dataclass
class RegionSummary:
    region_id: str
    measured_width: float
    material: str | None = None
    layer: str | None = None


@dataclass
class RegionSuggestion:
    region_id: str
    score: float  # model-reported; Chain D caps stored confidence at low regardless
    why: str = ""


@dataclass
class AICallLog:
    """Counts adapter invocations per run (max_calls_per_run budget + tests)."""
    calls: list = field(default_factory=list)

    def record(self, method: str, **kwargs):
        self.calls.append({"method": method, **kwargs})

    def count(self, method: str | None = None) -> int:
        if method is None:
            return len(self.calls)
        return sum(1 for c in self.calls if c.get("method") == method)


class Adapter:
    """Firm backend contract. All I/O is plain data; crops pass as PNG bytes."""

    def group_text_lines(self, crop_png: bytes | None, lines: list[TextLine],
                         leader_count: int) -> list[list[int]]:
        raise NotImplementedError

    def attribute_dimension(self, crop_png: bytes | None, dimension_value: float,
                            dimension_label: str,
                            candidate_regions: list[RegionSummary]) -> list[RegionSuggestion]:
        raise NotImplementedError


class DisabledAdapter(Adapter):
    def group_text_lines(self, crop_png, lines, leader_count):
        raise AIDisabled("ai.enabled is false — using geometric grouping")

    def attribute_dimension(self, crop_png, dimension_value, dimension_label,
                            candidate_regions):
        raise AIDisabled("ai.enabled is false — dimension stays unattributed")


def validate_partition(groups: list[list[int]], n_lines: int) -> list[list[int]]:
    """Addendum E: never accept a grouping that is not a strict partition of
    the supplied indices. Returns normalised (sorted) groups or raises."""
    if not isinstance(groups, list) or not groups:
        raise AIInvalidOutput("grouping must be a non-empty list of groups")
    flat: list[int] = []
    for g in groups:
        if not isinstance(g, list) or not g:
            raise AIInvalidOutput("each group must be a non-empty list of indices")
        for i in g:
            if not isinstance(i, int) or isinstance(i, bool):
                raise AIInvalidOutput(f"line index must be int, got {i!r}")
            flat.append(i)
    if sorted(flat) != list(range(n_lines)):
        raise AIInvalidOutput(
            f"not a partition of 0..{n_lines - 1}: got {sorted(flat)}")
    return [sorted(g) for g in groups]


def validate_region_ranking(suggestions, candidate_ids: list[str]) -> list[RegionSuggestion]:
    """Addendum E: the model chooses only between numbered options arithmetic
    did not already exclude. Unknown ids invalidate the whole response."""
    if not isinstance(suggestions, list) or not suggestions:
        raise AIInvalidOutput("ranking must be a non-empty list")
    out = []
    for s in suggestions:
        rid = s.region_id if isinstance(s, RegionSuggestion) else s.get("region_id")
        score = s.score if isinstance(s, RegionSuggestion) else s.get("score")
        why = (s.why if isinstance(s, RegionSuggestion) else s.get("why")) or ""
        if rid not in candidate_ids:
            raise AIInvalidOutput(f"unknown region id {rid!r} not in {candidate_ids}")
        try:
            score = float(score)
        except (TypeError, ValueError):
            raise AIInvalidOutput(f"non-numeric score for {rid!r}")
        out.append(RegionSuggestion(region_id=rid, score=score, why=str(why)))
    return out


def load_ai_config(config_dir: str | Path | None = None) -> dict:
    """config/ai.yaml with bundled fallback (mirrors semantics.load_config_dir)."""
    defaults = {"enabled": False, "backend": "disabled", "max_calls_per_run": 200}
    custom = Path(config_dir) / "ai.yaml" if config_dir else None
    if custom is not None and custom.exists():
        try:
            import yaml
            with open(custom, "r", encoding="utf-8") as f:
                return {**defaults, **(yaml.safe_load(f) or {})}
        except Exception:
            return defaults
    try:
        from importlib import resources
        bundled = resources.files("gitail") / "config" / "ai.yaml"
        if bundled.is_file():
            import yaml
            with open(bundled, "r", encoding="utf-8") as f:
                return {**defaults, **(yaml.safe_load(f) or {})}
    except Exception:
        pass
    return defaults


def load_adapter(config_dir: str | Path | None = None,
                 call_log: AICallLog | None = None):
    """(adapter, enabled, budget_left_callable). Unknown backends resolve to
    DisabledAdapter — a misconfigured name must never crash indexing."""
    cfg = load_ai_config(config_dir)
    enabled = bool(cfg.get("enabled", False))
    budget = int(cfg.get("max_calls_per_run", 200) or 0)
    log = call_log if call_log is not None else AICallLog()
    name = str(cfg.get("backend") or "disabled").lower()
    adapter: Adapter = DisabledAdapter()
    if enabled and name != "disabled":
        try:
            from importlib import metadata
            eps = metadata.entry_points(group="gitail.ai_backends")
            match = [e for e in eps if e.name == name]
            if match:
                adapter = match[0].load()(call_log=log)
        except Exception:
            adapter = DisabledAdapter()

    def budget_left() -> bool:
        return budget <= 0 or log.count() < budget

    return adapter, enabled, budget_left, log


def make_ai_ctx(config_dir: str | Path | None = None,
                call_log: AICallLog | None = None) -> dict:
    """One dict to thread through extract/segment/attribute: {"adapter",
    "enabled", "budget_left", "log"}. Disabled by default — callers never
    branch on it; run_chain_d and signal 5 check enabled/budget themselves."""
    adapter, enabled, budget_left, log = load_adapter(config_dir, call_log)
    return {"adapter": adapter, "enabled": enabled,
            "budget_left": budget_left, "log": log}
