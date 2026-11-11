"""Read connection component selections from their own rows in the emitted HR report."""
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path
import re


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

    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag in ("h1","h2","h3","h4","h5"):
            self.current=None
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

    def handle_endtag(self,tag):
        if tag=="h4" and self.header is not None:
            heading=plain("".join(self.header))
            ident,_,rest=heading.partition(" — ")
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
            self.row=None
        elif tag=="table":
            self.table=False
            self.current=None


def check_report_selections(report_path,calc):
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
        if displayed_text(actual[0][0])!=expected:
            findings.append(dict(connection=ident,issue="printed components differ from saved selection",
                                 expected=[expected],actual=actual[0][0]))
    for key in parser.rows:
        findings.append(dict(connection=key[1],issue="printed component row has no matching saved record"))
    return dict(passed=not findings,connection_component_rows_checked=checked,member_sections_checked=members,findings=findings)
