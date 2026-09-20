"""DXF -> PDF rendering for viewing (never parsed — parsing stays DXF-only).

Browsers show .dxf as text; PDFs render the actual geometry. Every drawing
gets a committed preview at pdf/<drawing>.pdf mirrored from drawings/.
Requires matplotlib (headless Agg backend) + ezdxf drawing addon.
"""
from __future__ import annotations

from pathlib import Path


def render_dxf_to_pdf(dxf_path: Path, pdf_path: Path) -> bool:
    """Render one DXF modelspace to PDF. Returns True on success, False on skip/fail."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    dxf_path, pdf_path = Path(dxf_path), Path(pdf_path)
    try:
        import ezdxf
        doc = ezdxf.readfile(str(dxf_path))
    except Exception:
        return False
    try:
        fig = plt.figure(dpi=150)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_aspect("equal")
        ax.axis("off")
        _draw_layout(doc, doc.modelspace(), ax)
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(str(pdf_path), format="pdf", bbox_inches="tight")
        plt.close(fig)
        return True
    except Exception:
        try:
            plt.close("all")
        except Exception:
            pass
        return False


def _draw_layout(doc, msp, ax) -> None:
    """Draw modelspace via the ezdxf Frontend for LIGHT backgrounds: white
    CAD lines swap to black (COLOR_SWAP_BW) so previews are never blank.
    Some DIMENSION virtual blocks (notably programmatically built ones)
    crash the frontend with AttributeError — previews are viewing aids, so
    fall back to rendering without dimensions rather than failing the sheet.
    """
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.config import ColorPolicy, Configuration
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    config = Configuration(color_policy=ColorPolicy.COLOR_SWAP_BW)
    backend = MatplotlibBackend(ax, adjust_figure=False)
    try:
        Frontend(RenderContext(doc), backend, config=config).draw_layout(
            msp, finalize=True)
        return
    except Exception:
        pass
    for e in list(msp):
        try:
            if e.dxftype() == "DIMENSION":
                msp.delete_entity(e)
        except Exception:
            pass
    Frontend(RenderContext(doc), MatplotlibBackend(ax, adjust_figure=False),
             config=config).draw_layout(msp, finalize=True)


def iter_drawings(drawings_dir: Path) -> list[Path]:
    drawings_dir = Path(drawings_dir)
    if not drawings_dir.exists():
        return []
    return sorted([p for p in drawings_dir.rglob("*") if p.suffix.lower() == ".dxf"])


def drawing_id(dxf: Path, drawings_dir: Path) -> str:
    try:
        return dxf.resolve().relative_to(Path(drawings_dir).resolve()).with_suffix("").as_posix()
    except ValueError:
        return dxf.stem


def render_all(drawings_dir: Path, pdf_dir: Path, force: bool = False) -> list[dict]:
    """Render every DXF missing/outdated in pdf/. Returns per-drawing dicts."""
    drawings_dir, pdf_dir = Path(drawings_dir), Path(pdf_dir)
    results = []
    for dxf in iter_drawings(drawings_dir):
        did = drawing_id(dxf, drawings_dir)
        pdf = pdf_dir / (did + ".pdf")
        outdated = (not pdf.exists() or
                    pdf.stat().st_mtime < dxf.stat().st_mtime)
        if not outdated and not force:
            results.append({"drawing": did, "dxf": str(dxf), "pdf": str(pdf), "action": "up-to-date"})
            continue
        ok = render_dxf_to_pdf(dxf, pdf)
        results.append({"drawing": did, "dxf": str(dxf), "pdf": str(pdf),
                        "action": "rendered" if ok else "FAILED"})
    return results


def check_all(drawings_dir: Path, pdf_dir: Path) -> list[str]:
    """IDs whose PDF is missing or older than the DXF (CI --check)."""
    missing = []
    for dxf in iter_drawings(drawings_dir):
        did = drawing_id(dxf, drawings_dir)
        pdf = Path(pdf_dir) / (did + ".pdf")
        if not pdf.exists() or pdf.stat().st_mtime < dxf.stat().st_mtime:
            missing.append(did)
    return missing


# --------------------------------------------------------------------------
# 7.6 thumbnails + 8.7.2 evidence crops (viewing only — parsing stays DXF).
#
# PNG bytes are NOT deterministic across matplotlib versions, so CI --check
# asserts existence only, never byte equality. Rendering never blocks ingest:
# every helper returns None/False on failure and the drawing still indexes.


def render_window(dxf_path: Path, bbox: list[float], out_png: Path,
                  highlight: list[float] | None = None,
                  size_px: int = 400,
                  overlays: list[dict] | None = None) -> bool:
    """Render modelspace windowed to bbox [x0,y0,x1,y1] (model mm) with an
    optional red highlight rectangle. Square output, equal aspect: the smaller
    dimension is expanded so geometry never stretches.

    overlays (Chain D crops): [{polygon: [(x,y)...], label: "1"}] drawn as
    numbered outlines so a model chooses between visible numbered options.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import ezdxf

    try:
        x0, y0, x1, y1 = (float(v) for v in bbox)
        if not (x1 > x0 and y1 > y0):
            return False
        doc = ezdxf.readfile(str(dxf_path))
        msp = doc.modelspace()
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        side = max(x1 - x0, y1 - y0)
        half = side / 2.0
        fig = plt.figure(figsize=(size_px / 100.0, size_px / 100.0), dpi=100)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_aspect("equal")
        ax.axis("off")
        _draw_layout(doc, msp, ax)
        ax.set_xlim(cx - half, cx + half)
        ax.set_ylim(cy - half, cy + half)
        if highlight is not None:
            try:
                import matplotlib.patches as patches
                hx0, hy0, hx1, hy1 = (float(v) for v in highlight)
                ax.add_patch(patches.Rectangle(
                    (hx0, hy0), hx1 - hx0, hy1 - hy0,
                    fill=False, edgecolor="red", linewidth=2.0))
            except Exception:
                pass
        if overlays:
            try:
                import matplotlib.patches as patches
                colors = ["#e41a1c", "#377eb8", "#4daf4a", "#984ea3",
                          "#ff7f00", "#a65628", "#f781bf", "#999999"]
                for i, ov in enumerate(overlays):
                    poly = [(float(p[0]), float(p[1]))
                            for p in (ov.get("polygon") or [])]
                    if len(poly) < 3:
                        continue
                    color = colors[i % len(colors)]
                    ax.add_patch(patches.Polygon(
                        poly, fill=False, edgecolor=color, linewidth=1.5))
                    xs = [p[0] for p in poly]
                    ys = [p[1] for p in poly]
                    ax.text(sum(xs) / len(xs), sum(ys) / len(ys),
                            str(ov.get("label", i + 1)), color="white",
                            fontsize=9, fontweight="bold",
                            ha="center", va="center",
                            bbox={"facecolor": color, "edgecolor": "none",
                                  "boxstyle": "circle,pad=0.3"})
            except Exception:
                pass
        out_png = Path(out_png)
        out_png.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(str(out_png), format="png")
        plt.close(fig)
        return True
    except Exception:
        try:
            import matplotlib.pyplot as plt
            plt.close("all")
        except Exception:
            pass
        return False


def render_window_bytes(dxf_path: Path, bbox: list[float],                        highlight: list[float] | None = None,
                        size_px: int = 400,
                        overlays: list[dict] | None = None) -> bytes | None:
    """render_window to memory (Chain D adapter input). None on failure."""
    import io
    import tempfile
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp = f.name
        try:
            if render_window(dxf_path, bbox, Path(tmp), highlight, size_px,
                             overlays):
                with open(tmp, "rb") as f:
                    return f.read()
            return None
        finally:
            try:
                Path(tmp).unlink()
            except OSError:
                pass
    except Exception:
        return None


def dim_crop_fn(dxf_path: Path, half_mm: float = 60.0):
    """Crop factory for Chain D: (anchor, candidate_regions) -> PNG bytes.

    The crop is prepared deliberately — dimension context with each candidate
    region outlined and numbered, measured widths printed in the review row —
    so the model chooses between numbered options against a visible number.
    """
    def _crop(anchor, regions) -> bytes | None:
        try:
            ax, ay = float(anchor[0]), float(anchor[1])
        except (TypeError, ValueError, IndexError):
            return None
        overlays = []
        for i, r in enumerate(regions or []):
            poly = r.get("polygon") if isinstance(r, dict) else None
            if poly and len(poly) >= 3:
                overlays.append({"polygon": poly, "label": str(i + 1)})
        win = [ax - half_mm, ay - half_mm, ax + half_mm, ay + half_mm]
        return render_window_bytes(dxf_path, win, overlays=overlays or None)
    return _crop


def phash_hex(png_path: Path) -> str | None:
    """256-bit average hash of a rendered PNG (64 hex chars). No imaging
    libs: block-average down to 16x16, threshold at the global mean.

    Sparse CAD linework needs the wider grid: an 8x8 difference hash lands
    unrelated details within a few bits of each other (all blank), while
    16x16 separates identical (0) from shifted (~13) from different (~60).
    """
    try:
        import matplotlib.image as mpimg
        img = mpimg.imread(str(png_path))
        if img.ndim == 3:
            img = img[..., :3].mean(axis=2)
        h, w = img.shape
        cells = []
        for r in range(16):
            for c in range(16):
                blk = img[int(r * h / 16):int((r + 1) * h / 16),
                          int(c * w / 16):int((c + 1) * w / 16)]
                cells.append(float(blk.mean()))
        mean = sum(cells) / len(cells)
        bits = "".join("1" if v > mean else "0" for v in cells)
        return f"{int(bits, 2):064x}"
    except Exception:
        return None


def dhash_hex(png_path: Path) -> str | None:
    """Legacy alias (8x8 difference hash) — prefer phash_hex for CAD."""
    return phash_hex(png_path)


def hamming(a: str, b: str) -> int:
    """Bit distance between two hex hashes (near-duplicate ranking)."""
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except (TypeError, ValueError):
        return 64


NEAR_DUP_BITS = 32  # hamming distance (of 256) at/below which details flag as near-duplicates
CROP_CONTEXT_MM = 30.0  # model-space context around a quarantined element (8.7.2)
DIM_CROP_HALF_MM = 60.0  # Chain D crop half-window around the dimension anchor


def _store_path(root_dir: Path, out: Path) -> str:
    """Repo-stable committed path: relative to the visuals root's PARENT, so
    thumbs/StageC/D-102.d_a41f.png resolves from any repo root by joining —
    never an absolute machine path."""
    try:
        return (Path(root_dir.name) / out.relative_to(root_dir)).as_posix()
    except ValueError:
        return out.as_posix()


def finalize_visuals(dxf_path: Path, payload: dict, records_by_eid: dict,
                     thumbs_dir: Path, crops_dir: Path,
                     drawing: str | None = None,
                     dry_run: bool = False) -> dict:
    """Render detail thumbnails + quarantine crops for one extracted sheet and
    patch them back into the payload (thumbnail/phash/near_duplicates/crop).

    Returns {"payload", "files"}; files are absolute Paths for the committer.
    Pure ingest convenience — failures degrade to None fields, never errors.
    dry_run computes the expected file list without writing (CI --check).
    """
    from .segment import _anchor, _bbox_of  # record geometry helpers
    drawing = drawing or payload.get("drawing")
    files: list[Path] = []
    details = payload.get("details", []) or []
    if dry_run:
        for d in details:
            files.append(Path(thumbs_dir) / (drawing + f".{d.get('detail_id')}.png"))
        for cand in payload.get("quarantine_candidates", []) or []:
            rec = records_by_eid.get(cand.get("element_id")) if cand.get("element_id") else None
            if rec is not None and _anchor(rec) is not None:
                files.append(Path(crops_dir) / (drawing + f".{cand['element_id']}.png"))
        for rv in payload.get("dim_review", []) or []:
            if rv.get("anchor"):
                files.append(Path(crops_dir) / (drawing + f".{rv.get('uid', 'dim')}.png"))
        return {"payload": payload, "files": files}
    details = payload.get("details", []) or []
    # thumbnails: one per detail region (7.6), hash for near-dup clustering.
    for d in details:
        bbox = d.get("sheet_region_bbox") or [0, 0, 100, 100]
        try:
            pad = max(bbox[2] - bbox[0], bbox[3] - bbox[1]) * 0.05
            win = [bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad]
        except (TypeError, IndexError):
            win = bbox
        out = Path(thumbs_dir) / (drawing + f".{d.get('detail_id')}.png")
        if render_window(dxf_path, win, out):
            d["thumbnail"] = _store_path(Path(thumbs_dir), out)
            d["phash"] = phash_hex(out)
            files.append(out)
    # near-duplicates: high hash similarity flags, never merges (7.9 #10).
    hashed = [d for d in details if d.get("phash")]
    for i, a in enumerate(hashed):
        dupes = sorted(b["detail_id"] for j, b in enumerate(hashed)
                       if j != i and hamming(a["phash"], b["phash"]) <= NEAR_DUP_BITS)
        a["near_duplicates"] = dupes
    # evidence crops: quarantined element highlighted in context (8.7.2).
    for cand in payload.get("quarantine_candidates", []) or []:
        eid = cand.get("element_id")
        rec = records_by_eid.get(eid) if eid else None
        anchor = _anchor(rec) if rec else None
        if anchor is None:
            continue
        win = [anchor[0] - CROP_CONTEXT_MM, anchor[1] - CROP_CONTEXT_MM,
               anchor[0] + CROP_CONTEXT_MM, anchor[1] + CROP_CONTEXT_MM]
        box = _bbox_of(rec) if rec else None
        out = Path(crops_dir) / (drawing + f".{eid}.png")
        if render_window(dxf_path, win, out, highlight=box):
            cand["crop"] = _store_path(Path(crops_dir), out)
            files.append(out)
    # Chain D crops: dimension anchor in context with candidate regions
    # outlined and numbered, measured widths carried in the review row.
    for rv in payload.get("dim_review", []) or []:
        anchor = rv.get("anchor")
        if not anchor:
            continue
        try:
            ax, ay = float(anchor[0]), float(anchor[1])
        except (TypeError, ValueError, IndexError):
            continue
        win = [ax - DIM_CROP_HALF_MM, ay - DIM_CROP_HALF_MM,
               ax + DIM_CROP_HALF_MM, ay + DIM_CROP_HALF_MM]
        out = Path(crops_dir) / (drawing + f".{rv.get('uid', 'dim')}.png")
        if render_window(dxf_path, win, out,
                         overlays=rv.get("overlays") or None):
            rv["crop"] = _store_path(Path(crops_dir), out)
            files.append(out)
    return {"payload": payload, "files": files}
