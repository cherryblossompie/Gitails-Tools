"""Build tests/fixtures/architrave.dxf — the Part 7 reference sheet.

Two detail regions (AR 1 plasterboard build-up, AR 2 aluminium), title markers
(CIRCLE + TEXT + title + Scale: 1:5), leader callouts, two dimensions (one
bound, one floating), cross-reference text. Model units are mm, 1:1.
"""
from pathlib import Path

import ezdxf

OUT = Path(__file__).parent / "architrave.dxf"


def rect(msp, x0, y0, x1, y1, layer):
    msp.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
                       close=True, dxfattribs={"layer": layer})


def build() -> Path:
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    anno = "A-DETL-ANNO"

    msp.add_mtext("TYPICAL ARCHITRAVE DETAILS", dxfattribs={"layer": anno}).dxf.insert = (100, 175)

    # ---- AR 1: plasterboard region x[100,110] y[0,100] ----
    rect(msp, 100, 0, 110, 100, "A-DETL-PLASTER")
    h = msp.add_hatch(dxfattribs={"layer": "A-DETL-PLASTER"})
    h.set_pattern_fill("PLASTER")  # deliberately unmapped: material comes from the layer
    h.paths.add_polyline_path([(100, 0), (110, 0), (110, 100), (100, 100)], is_closed=True)

    msp.add_mtext("NOM. 10MM FLUSH JOINTED PLASTERBOARD",
                  dxfattribs={"layer": anno}).dxf.insert = (40, 50)
    msp.add_line((58, 50), (105, 50), dxfattribs={"layer": anno})  # leader into region

    msp.add_mtext("10MM QUIRK AT DOOR JAMB", dxfattribs={"layer": anno}).dxf.insert = (40, 80)
    msp.add_line((58, 80), (100, 80), dxfattribs={"layer": anno})  # leader to region edge

    msp.add_mtext("TYPICAL WET AREA WALL TILE ON SUBSTRATE",
                  dxfattribs={"layer": anno}).dxf.insert = (40, 20)  # no leader

    msp.add_mtext("REFER. A.109 + A.110 FOR BUILD UP DETAILS",
                  dxfattribs={"layer": anno}).dxf.insert = (40, 150)

    d1 = msp.add_linear_dim(base=(105, -12), p1=(100, 10), p2=(110, 10),
                            dxfattribs={"layer": anno}).dimension
    d1.dxf.text = "10"
    d2 = msp.add_linear_dim(base=(320, 300), p1=(300, 300), p2=(320, 300),
                            dxfattribs={"layer": anno}).dimension
    d2.dxf.text = "20"  # floating in whitespace: resolves to no region

    # ---- AR 2: aluminium loop x[200,206] y[0,100] (closed loop, no hatch) ----
    rect(msp, 200, 0, 206, 100, "A-DETL-ALUM")

    # ---- title markers: CIRCLE + TEXT inside + title + scale ----
    for cx, tag, title in ((155, "AR 1", "TYPICAL ARCHITRAVE DETAILS — AR 1"),
                           (255, "AR 2", "TYPICAL ARCHITRAVE DETAILS — AR 2")):
        msp.add_circle((cx, 130), 8, dxfattribs={"layer": anno})
        msp.add_text(tag, dxfattribs={"layer": anno, "height": 5}).set_placement((cx, 130))
        msp.add_mtext(title, dxfattribs={"layer": anno}).dxf.insert = (cx - 30, 145)
        msp.add_mtext("Scale: 1:5", dxfattribs={"layer": anno}).dxf.insert = (cx - 30, 118)

    doc.saveas(str(OUT))
    return OUT


if __name__ == "__main__":
    print("wrote", build(), OUT.stat().st_size)
