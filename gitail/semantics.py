"""Part 3 — semantic parsing of annotation strings and hatch patterns.

Driven entirely by config/materials.yaml. Never drops an element:
on parse failure returns None and the caller keeps text_raw searchable.
"""
from __future__ import annotations

import re
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


def load_materials(config_path: str | Path) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) if yaml else {}
    return data or {}


def load_config_dir(config_dir: str | Path) -> tuple[dict, str]:
    """Materials config with bundled fallback.

    Returns (cfg, source). Firms override via --config-dir; otherwise the
    configs shipped inside the package apply — so serve/CLI parse identically
    no matter which directory they start from. Without this, a server started
    in the drawings repo (which has no config/) silently parses nothing.
    """
    custom = Path(config_dir) / "materials.yaml"
    if custom.exists():
        return load_materials(custom), str(custom)
    try:
        from importlib import resources
        bundled = resources.files("gitail") / "config" / "materials.yaml"
        if bundled.is_file():
            return load_materials(bundled), "bundled defaults"
    except Exception:
        pass
    return {}, "empty (no config found)"


def _find_material(lower: str, materials: dict) -> str | None:
    """Longest-keyword match so 'plywood' beats 'ply'."""
    best = None
    best_len = 0
    for mat, spec in (materials or {}).items():
        for kw in (spec or {}).get("keywords", []) or []:
            kw_l = str(kw).lower()
            if kw_l and kw_l in lower and len(kw_l) > best_len:
                best = mat
                best_len = len(kw_l)
    return best


def _find_qualifier(lower: str, cfg: dict, materials: dict) -> str | None:
    quals: list[str] = list(cfg.get("qualifiers", []) or [])
    for spec in (materials or {}).values():
        for q in (spec or {}).get("qualifiers", []) or []:
            if q not in quals:
                quals.append(q)
    for q in quals:
        if str(q).lower() in lower:
            return str(q).lower()
    return None


def _find_profile(raw: str, materials: dict) -> str | None:
    profiles: list[str] = []
    for spec in (materials or {}).values():
        profiles.extend((spec or {}).get("profiles", []) or [])
    if not profiles:
        profiles = ["SHS", "RHS", "UB", "UC", "PFC", "EA", "UA"]
    m = re.search(r"\b(" + "|".join(re.escape(p) for p in profiles) + r")\b", raw, re.IGNORECASE)
    return m.group(1).upper() if m else None


def _find_part(raw: str, cfg: dict) -> str | None:
    """Component noun: door, lining, decking... Longest match wins so
    'architrave' beats 'rave'-style accidents; word boundaries + optional
    plural keep 'WALLS:' -> wall. Returns the singular config form."""
    parts = [str(p) for p in (cfg.get("parts", []) or []) if str(p).strip()]
    best = None
    best_len = 0
    for part in parts:
        m = re.search(r"\b" + re.escape(part) + r"s?\b", raw, re.IGNORECASE)
        if m and len(part) > best_len:
            best, best_len = part.lower(), len(part)
    return best


def parse_text(text_raw: str | None, cfg: dict) -> dict | None:
    """Parse an annotation string into structured fields.

    Returns None when no material can be identified (caller keeps text_raw).
    """
    if not text_raw or not str(text_raw).strip():
        return None
    raw = str(text_raw).strip()
    lower = raw.lower()
    materials = cfg.get("materials", {}) or {}

    profile = _find_profile(raw, materials)

    # Profile dims like 75x50 SHS — return early, thickness does not apply.
    if profile:
        dm = re.search(r"(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)", raw)
        dims = [float(dm.group(1)), float(dm.group(2))] if dm else None
        material = _find_material(lower, materials) or "steel"
        out: dict = {"material": material, "profile": profile}
        if dims:
            # keep ints as ints when whole numbers for readability
            out["dims"] = [int(d) if float(d).is_integer() else float(d) for d in dims]
        q = _find_qualifier(lower, cfg, materials)
        if q:
            out["qualifier"] = q
        part = _find_part(raw, cfg)
        if part:
            out["part"] = part
        return out

    material = _find_material(lower, materials)
    part = _find_part(raw, cfg)

    # R-value e.g. "R2.5 BATT INSUL"
    r_match = re.search(r"\bR\s*(\d+(?:\.\d+)?)\b", raw, re.IGNORECASE)
    if r_match and (material == "insulation" or "insul" in lower):
        out = {"material": material or "insulation", "r_value": float(r_match.group(1))}
        q = _find_qualifier(lower, cfg, materials)
        if q:
            out["qualifier"] = q
        if part:
            out["part"] = part
        return out

    if material is None:
        # No material, but a recognizable component ("ENTRY MAT") — keep the
        # part so the element stays categorized instead of unparseable.
        if part:
            out = {"part": part}
            q = _find_qualifier(lower, cfg, materials)
            if q:
                out["qualifier"] = q
            return out
        return None

    # Thickness: prefer number with mm/THK unit, else first bare number.
    # Handles "2mm GLASS", "GLASS 2mm", "12 THK PLYWOOD", "6.38 LAMINATED".
    candidates = list(re.finditer(r"(\d+(?:\.\d+)?)\s*(mm|thk|cm|m)?\b", raw, re.IGNORECASE))
    value = None
    unit = None
    if candidates:
        with_unit = [c for c in candidates if (c.group(2) or "").lower() in ("mm", "thk")]
        pick = with_unit[0] if with_unit else candidates[0]
        num = float(pick.group(1))
        u = (pick.group(2) or "").lower()
        if u == "cm":
            num *= 10.0
            unit = "mm"
        elif u == "m":
            num *= 1000.0
            unit = "mm"
        elif u in ("mm", "thk", ""):
            unit = "mm"
        else:
            unit = u
        value = int(num) if float(num).is_integer() else num

    out = {"material": material}
    if value is not None:
        out["value"] = value
        out["unit"] = unit or "mm"
    q = _find_qualifier(lower, cfg, materials)
    if q:
        out["qualifier"] = q
    if part:
        out["part"] = part
    return out


def parse_hatch(pattern_name: str | None, cfg: dict) -> str | None:
    if not pattern_name:
        return None
    hatch_map = cfg.get("hatch_map", {}) or {}
    # case-insensitive lookup
    upper = str(pattern_name).upper()
    for k, v in hatch_map.items():
        if str(k).upper() == upper:
            return v
    return None


def parse_annotation(text_raw: str | None, hatch_pattern: str | None, cfg: dict) -> dict | None:
    """Text first, hatch fallback. None means unparseable (still indexed via text_raw)."""
    parsed = parse_text(text_raw, cfg) if text_raw else None
    if parsed is not None:
        return parsed
    mat = parse_hatch(hatch_pattern, cfg)
    if mat:
        return {"material": mat}
    return None
