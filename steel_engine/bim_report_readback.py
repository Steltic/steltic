"""Read member sections, connection selections and governing D/C values from the emitted HR report."""
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path
import re

from .report_text import publish


def plain(value):
    return " ".join(value.split())


def displayed_text(value):
    s=plain(value).replace("<=","≤").replace(">=","≥")
    s=re.sub(r"\b(?:Omega0|Om0)\b","Ω₀",s)
    greek={"delta":"δ","theta":"θ","rho":"ρ","omega":"ω","alpha":"α","beta":"β",
           "gamma":"γ","lambda":"λ","psi":"ψ","mu":"μ"}
    s=re.sub(r"\b("+"|".join(greek)+r")_([a-z]{1,3})\b",lambda m:greek[m[1]]+m[2],s)
    s=re.sub(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)(?=\s*=)",lambda m:m[1].replace("_"," "),s)
    s=re.sub(r"\bsqrt\(","√(",s)
    s=re.sub(r"\bthe brief\b","the project criteria",s)
    return re.sub(r"([;,(]\s*)brief\b",r"\1project criteria",s)


class ConnectionRows(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows=defaultdict(list)
        self.members=defaultdict(list)
        self.current=None
        self.header=None
        self.key=None
        self.table=False
        self.row=None
        self.cell=None
        # design schedule read-back: the block a paragraph belongs to, its governing line, its issued
        # selection and the summary-table rows that link to it
        self.block=None
        self.bold=None
        self.depth=0
        self.governing=defaultdict(list)
        self.issued=defaultdict(list)
        self.summary=defaultdict(list)
        self.srow=None
        self.scell=None
        self.shref=None

    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=="tr":
            self.srow=[];self.shref=None
        elif tag in ("td","th") and self.srow is not None:
            self.scell=[]
        elif tag=="a" and self.scell is not None and not self.srow and attrs.get("href","").startswith(("#m-","#c-")):
            self.shref=attrs["href"][1:]
        elif tag=="b" and self.block is not None:
            if self.bold is None:           # a status inside the governing line is bold too
                self.bold=[];self.depth=0
            self.depth+=1
        elif tag=="br" and self.scell is not None:
            self.scell.append(" ")
        if tag in ("h1","h2","h3","h4","h5"):
            self.current=None
            if tag!="h5":                   # a calculation block keeps its sub-headings
                self.block=None
            self.key=attrs.get("id","")
            self.header=[] if tag=="h4" and self.key.startswith(("c-","m-")) else None
        elif tag=="table":
            self.table=self.current is not None and {"calc","kv"}<=set(attrs.get("class","").split())
        elif tag=="tr" and self.table:
            self.row=[]
        elif tag=="td" and self.row is not None:
            self.cell=[]
        elif tag=="br" and self.cell is not None:
            self.cell.append(" ")

    def handle_data(self,data):
        if self.header is not None:
            self.header.append(data)
        if self.cell is not None:
            self.cell.append(data)
        if self.scell is not None:
            self.scell.append(data)
        if self.bold is not None:
            self.bold.append(data)

    def handle_endtag(self,tag):
        if tag=="h4" and self.header is not None:
            heading=plain("".join(self.header))
            ident,_,rest=heading.partition(" — ")
            self.block=(self.key,ident)
            if self.key.startswith("m-"):
                self.members[(self.key,ident)].append(rest.rsplit(" (",1)[0])
            else:
                self.current=(self.key,ident)
            self.header=None
        elif tag=="td" and self.cell is not None:
            self.row.append(plain("".join(self.cell)))
            self.cell=None
        elif tag=="tr" and self.row is not None:
            if self.current and self.row and self.row[0]=="Components":
                self.rows[self.current].append(self.row[1:] if len(self.row)>1 else [])
            if self.current and self.row and self.row[0]=="Issued selection":
                self.issued[self.current].append(" ".join(self.row[1:]))
            self.row=None
        elif tag=="table":
            self.table=False
            self.current=None
        if tag in ("td","th") and self.scell is not None:
            self.srow.append(plain("".join(self.scell)))
            self.scell=None
        elif tag=="tr" and self.srow is not None:
            if self.shref:
                self.summary[self.shref].append(self.srow)
            self.srow=None
        elif tag=="b" and self.bold is not None:
            self.depth-=1
            if self.depth:
                return
            text=plain("".join(self.bold))
            if text.startswith("Governing D/C =") and self.block is not None:
                self.governing[self.block].append(text[len("Governing D/C ="):].split(" — ")[0].strip())
            self.bold=None


def check_report_selections(report_path,calc,design_schedule=True):
    """Compare the report with the saved calculation records. ``design_schedule=False`` reads member
    sections and connection components only: the scope for an archived report of an earlier renderer."""
    parser=ConnectionRows()
    parser.feed(Path(report_path).read_text(encoding="utf-8-sig"))
    parser.close()
    findings=[]
    checked=0
    members=0
    for record in calc.get("members",[]):
        if not isinstance(record,dict):
            continue
        selected=(record.get("inputs") or {}).get("section")
        if not selected:
            continue
        ident=str(record.get("id",""))
        key=("m-"+re.sub(r"[^A-Za-z0-9_-]","_",ident),ident)
        actual=parser.members.pop(key,[])
        members+=1
        if actual!=[str(selected)]:
            findings.append(dict(member=ident,issue="printed member section differs from saved selection",
                                 expected=[str(selected)],actual=actual))
    for key in parser.members:
        findings.append(dict(member=key[1],issue="printed member section has no matching saved record"))
    rows=calc.get("connections") or calc.get("connection_demands",[])
    if isinstance(rows,dict):
        rows=rows.items()
    else:
        rows=[(r.get("id",""),r) for r in rows if isinstance(r,dict)]
    for raw_id,record in rows:
        value=record.get("components",record.get("notes"))
        if not value:
            continue
        checked+=1
        ident=str(raw_id)
        key=("c-"+re.sub(r"[^A-Za-z0-9_-]","_",ident),ident)
        actual=parser.rows.pop(key,[])
        if len(actual)!=1 or len(actual[0])!=1:
            findings.append(dict(connection=ident,issue="missing, duplicated or misbound original report component row"))
            continue
        # The existing presentation rules can typeset symbols and replace internal references.
        # The stored calculation remains the source. A changed word, grade or numeric selection fails.
        expected=displayed_text(str(value))
        # Either the long-standing presentation rules, or exactly what the current renderer prints
        # for this saved text (identifiers, emphasis and internal references are typeset on output).
        if displayed_text(actual[0][0])!=expected and plain(actual[0][0])!=plain(publish(str(value))):
            findings.append(dict(connection=ident,issue="printed components differ from saved selection",
                                 expected=[expected],actual=actual[0][0]))
    for key in parser.rows:
        findings.append(dict(connection=key[1],issue="printed component row has no matching saved record"))
    # The design schedule issued to openBIM: every governing D/C and typed selection must be what the
    # report prints, in the calculation block and in every summary table row that links to it.
    if not design_schedule:
        return dict(passed=not findings,connection_component_rows_checked=checked,member_sections_checked=members,findings=findings)
    from .bim_design_schedule import design_schedule as printed_schedule
    schedule=printed_schedule(calc)
    ratios=summaries=issued=0
    for kind,rows in (("member",schedule["members"]),("connection",schedule["connections"])):
        for row in rows:
            key=(row["anchor"],row["id"])
            expected=[row["dc_text"]] if row["dc_text"] is not None else []
            actual=parser.governing.get(key,[])
            ratios+=1
            if actual!=expected and not (kind=="member" and not expected and len(actual)==1):
                findings.append({kind:row["id"],"issue":"printed governing D/C differs from the design schedule",
                                 "expected":expected,"actual":actual})
            for cells in parser.summary.get(row["anchor"],[]):
                summaries+=1
                if len(cells)<3 or cells[-2]!=(row["dc_text"] or "—"):
                    findings.append({kind:row["id"],"issue":"summary table D/C differs from the design schedule",
                                     "expected":[row["dc_text"]],"actual":cells[-2:-1]})
            if kind=="connection":
                expected=[plain(" ".join(row["selection"]))] if row["selection"] else []
                actual=[plain(v) for v in parser.issued.get(key,[])]
                issued+=bool(expected)
                if actual!=expected:
                    findings.append(dict(connection=row["id"],issue="printed issued selection differs from the typed selection",
                                         expected=expected,actual=actual))
    return dict(passed=not findings,connection_component_rows_checked=checked,member_sections_checked=members,
                governing_ratios_checked=ratios,summary_rows_checked=summaries,issued_selections_checked=issued,findings=findings)
