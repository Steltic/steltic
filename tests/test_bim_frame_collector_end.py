"""A collector-to-braced-frame record is the end connection where the collector meets the braced-frame column."""
from steel_engine.bim_source_fields import applications, frame_collector_ends


def field(role, name, value):
    return {"role": role, "field": name, "value": value, "applicability": {}}


def single_plate(source_type, count, diameter, grade, thickness, depth):
    return [field("connection", "variant", "single_plate_shear"), field("connection", "source_type", source_type),
            field("bolt", "count", count), field("bolt", "diameter_in", diameter), field("bolt", "material", grade),
            field("shear_plate", "thickness_in", thickness), field("shear_plate", "depth_in", depth)]


def records(collector_bolts=5, collector_depth=14.5):
    return {
        "connections/conn-roof-W21X55": {"fields": single_plate("single-plate shear connection with the collector axial force", 4, .875, "A325-N", .5, 11.5),
                                         "member_section": {"value": "W21X55"}},
        "connections/conn-collector-X": {"fields": single_plate("collector-to-braced-frame connection (single plate, shear + axial)",
                                                                collector_bolts, .875, "A325-N", .5, collector_depth),
                                         "application_target": {"role": "collector_to_frame"}},
    }


def site(beam="hr:element:36", direction="X", braced=(), frame=("X",), collector=True):
    sources = ["members/roof-W21X55"] + (["members/collector-X-W21X55"] if collector else [])
    return {"point_in": [600.0, 0.0, 384.0], "grids": ["2", "A"], "levels": [1], "columns": {}, "support_faces": [],
            "end_members": {beam: {"section": "W21X55", "kind": "beam", "direction": direction, "source_ids": sources}},
            "brace_frame_directions": list(frame), "braced_member_ids": list(braced), "corner_members": {}}


def joint(beam="hr:element:36"):
    return {"id": beam + ":1", "design_record_ids": ["connections/conn-roof-W21X55", "connections/conn-collector-X"],
            "member_ids": [beam, "hr:element:8"], "parameters": {}}


def selected(links):
    return {entry["record_id"].split("/")[1]: bool(entry["selected"]) for entry in links}


def test_collector_end_at_the_braced_frame_column_uses_the_collector_record():
    links = applications(joint(), records(), site())
    assert selected(links) == {"conn-roof-W21X55": False, "conn-collector-X": True}
    by_id = {entry["record_id"]: entry for entry in links}
    binding = by_id["connections/conn-collector-X"]["interface_application"]
    assert binding["status"] == "located_interface"
    assert binding["governs_beam_end_records"] == ["connections/conn-roof-W21X55"]
    assert by_id["connections/conn-roof-W21X55"]["located_specific_records"] == ["connections/conn-collector-X"]


def test_other_ends_keep_the_beam_end_record():
    cases = [site(frame=()),                          # the column is not a braced-frame column
             site(frame=("Y",)),                      # braced frame in the other direction only
             site(braced=("hr:element:36",)),         # the beam is the braced-bay beam, with its own gusset end
             site(direction="Y"),                     # a beam framing in from the other direction
             site(collector=False)]                   # not a collector member in the HR design
    for location in cases:
        assert frame_collector_ends(joint(), records(), location) == {}
        assert selected(applications(joint(), records(), location)) == {"conn-roof-W21X55": True, "conn-collector-X": False}


def test_a_weaker_collector_record_does_not_replace_the_beam_end_record():
    for weaker in (records(collector_bolts=3), records(collector_depth=9.0)):
        assert frame_collector_ends(joint(), weaker, site()) == {}
        assert selected(applications(joint(), weaker, site())) == {"conn-roof-W21X55": True, "conn-collector-X": False}
