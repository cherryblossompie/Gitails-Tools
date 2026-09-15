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
    import ezdxf
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    dxf_path, pdf_path = Path(dxf_path), Path(pdf_path)
    try:
        doc = ezdxf.readfile(str(dxf_path))
    except Exception:
        return False
    msp = doc.modelspace()
    try:
        fig = plt.figure(dpi=150)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_aspect("equal")
        ax.axis("off")
        ctx = RenderContext(doc)
        backend = MatplotlibBackend(ax)
        Frontend(ctx, backend).draw_layout(msp, finalize=True)
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
