"""GPX 1.0 writer for Velocitek trackpoints.

Mirrors the output of VelocitekControlCenter/TrackpointsToGpx.xslt — a
single <trk> containing one <trkseg>, with <bounds> covering the points and
a per-point <time>. Speed/heading are not part of standard GPX; the Mac app
drops them on the way out, and so do we.
"""

import xml.etree.ElementTree as ET

from velocitek import Trackpoint

GPX_NS = "http://www.topografix.com/GPX/1/0"


def _iso8601_z(ts) -> str:
    # GPX wants UTC with a literal 'Z' suffix, millisecond precision or better.
    return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"


def write_gpx(path: str, points: list[Trackpoint], name: str = "Track") -> int:
    if not points:
        raise ValueError("refusing to write empty GPX")

    lats = [p.latitude for p in points]
    lons = [p.longitude for p in points]

    ET.register_namespace("", GPX_NS)
    gpx = ET.Element(
        f"{{{GPX_NS}}}gpx",
        {"version": "1.0", "creator": "velocitek-py"},
    )

    ET.SubElement(gpx, f"{{{GPX_NS}}}name").text = name
    ET.SubElement(gpx, f"{{{GPX_NS}}}time").text = _iso8601_z(points[0].timestamp)
    ET.SubElement(
        gpx,
        f"{{{GPX_NS}}}bounds",
        {
            "minlat": f"{min(lats):.6f}",
            "minlon": f"{min(lons):.6f}",
            "maxlat": f"{max(lats):.6f}",
            "maxlon": f"{max(lons):.6f}",
        },
    )

    trk = ET.SubElement(gpx, f"{{{GPX_NS}}}trk")
    ET.SubElement(trk, f"{{{GPX_NS}}}name").text = name
    trkseg = ET.SubElement(trk, f"{{{GPX_NS}}}trkseg")

    for p in points:
        trkpt = ET.SubElement(
            trkseg,
            f"{{{GPX_NS}}}trkpt",
            {"lat": f"{p.latitude:.6f}", "lon": f"{p.longitude:.6f}"},
        )
        ET.SubElement(trkpt, f"{{{GPX_NS}}}time").text = _iso8601_z(p.timestamp)

    tree = ET.ElementTree(gpx)
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return len(points)
