"""Material rows shared by the HR report and the physical handoff."""
from html.parser import HTMLParser


class _MaterialsTable(HTMLParser):
    """Read the first table following the report's Materials heading."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.heading = None
        self.awaiting = False
        self.in_table = False
        self.done = False
        self.cell = None
        self.row = []
        self.rows = []

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if tag in ("h2", "h3", "h4") and not self.in_table:
            self.heading = []
        elif tag == "table" and self.awaiting:
            self.in_table = True
            self.awaiting = False
        elif self.in_table and tag == "tr":
            self.row = []
        elif self.in_table and tag == "td":
            self.cell = []
        elif self.cell is not None and tag == "br":
            self.cell.append(" ")

    def handle_data(self, data):
        if self.heading is not None:
            self.heading.append(data)
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if self.done:
            return
        if tag in ("h2", "h3", "h4") and self.heading is not None:
            self.awaiting = " ".join("".join(self.heading).split()).lower() == "materials"
            self.heading = None
        elif tag == "td" and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.in_table and len(self.row) >= 2:
            self.rows.append(tuple(self.row[:2]))
        elif tag == "table" and self.in_table:
            self.done = True
            self.in_table = False


def original_report_material_rows(path):
    parser = _MaterialsTable()
    with path.open(encoding="utf-8-sig") as stream:
        while not parser.done:
            block = stream.read(32768)
            if not block:
                break
            parser.feed(block)
    parser.close()
    return parser.rows
def _text(value):
    if isinstance(value, dict):
        return ", ".join(str(k) + " = " + _text(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return ", ".join(_text(v) for v in value)
    return str(value)


def _rows(declaration, origin):
    if isinstance(declaration, dict):
        for key, value in declaration.items():
            yield {"item": str(key), "spec": _text(value), "source": origin + "/" + str(key).replace("~", "~0").replace("/", "~1")}
    elif isinstance(declaration, (list, tuple)):
        for index, value in enumerate(declaration):
            if isinstance(value, (list, tuple)) and len(value) >= 2:
                label, spec = value[0], " ".join(_text(v) for v in value[1:])
            elif isinstance(value, dict):
                label = value.get("item") or value.get("element") or value.get("source_item") or value.get("id")
                spec = value.get("spec") or value.get("specification") or value.get("value")
                if value.get("props"):
                    spec = _text(spec) + " – " + _text(value["props"]) if spec is not None else None
            else:
                continue
            if label is not None and spec is not None:
                yield {"item": _text(label), "spec": _text(spec), "source": origin + "/" + str(index)}


def material_schedule(cfg, calc, physical_inputs=None):
    """Preserve named material selections and report competing assignments."""
    cfg, calc = cfg or {}, calc or {}
    rows = []
    conflicts = []
    selected = {}
    for origin, declaration in (("cfg.py#/materials", cfg.get("materials")),
                                ("design/calc_package.json#/materials", calc.get("materials")),
                                ("design/calc_package.json#/bim/materials", (calc.get("bim") or {}).get("materials")),
                                ("cfg.py#/bim/materials", (cfg.get("bim") or {}).get("materials")),
                                ("physical_inputs.json#/declarations/materials",
                                 ((physical_inputs or {}).get("declarations") or {}).get("materials"))):
        for row in _rows(declaration, origin):
            key = row["item"].strip().casefold()
            previous = selected.get(key)
            if previous and previous["spec"] == row["spec"]:
                continue
            if previous:
                conflicts.append({"role": row["item"], "sources": [previous["source"], row["source"]],
                                  "values": [previous["spec"], row["spec"]]})
            rows.append(row)
            selected[key] = row
    # A job may select several pin or bolt grades. Keep the exact connection
    # record and end scope instead of combining them into a default grade.
    from .bim_source_fields import selection_record
    grouped = {}
    for i, data in enumerate(calc.get("connections", [])):
        if not isinstance(data, dict) or data.get("waived"):
            continue
        rid = "connections/" + str(data.get("id", "@" + str(i)))
        record = {"id": rid, "table": "connections", "source_pointer": "/connections/" + str(i), "data": data}
        for field in selection_record(record)["fields"]:
            if field["field"] != "material" or not isinstance(field["value"], str):
                continue
            key = (field["role"], field["value"])
            row = grouped.setdefault(key, {"item": field["role"].replace("_", " "), "spec": field["value"],
                                          "source": "design/calc_package.json#" + field["source"]["pointer"],
                                          "applications": []})
            application = {"record_id": rid, "scope": field["applicability"], "source": field["source"]}
            if application not in row["applications"]:
                row["applications"].append(application)
    rows.extend(grouped.values())
    return {"materials": rows, "conflicts": conflicts,
            "status": "source_conflict" if conflicts else ("selected" if rows else "unresolved")}
