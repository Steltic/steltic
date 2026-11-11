import pytest
from steel_engine.bim_report_readback import check_report_selections


@pytest.mark.parametrize("defect",["none","size","material","wrong_key","swap","missing","duplicate","section"])
def test_connection_components_and_member_section_in_actual_report(tmp_path,defect):
    first="3/4-in plate and 4 A490 bolts, length >= 6 in"
    second="1/2-in gusset, 3 A325 bolts"
    calc=dict(members=[dict(id="column",inputs=dict(section="W12X40"))],
              connections=[dict(id="A",components=first),dict(id="B",components=second)])
    visible=[first.replace(">=","&ge;"),second]
    keys=["A","B"]
    if defect=="size":
        visible[0]=visible[0].replace("3/4","1/4")
    elif defect=="material":
        visible[0]=visible[0].replace("A490","A325")
    elif defect=="wrong_key":
        keys.reverse()
    elif defect=="swap":
        visible.reverse()
    block="<h4 id='c-{key}'>{label} &mdash; connection</h4><table class='calc kv'><tr><td>Components</td><td>{value}</td></tr></table>"
    rows=[block.format(key=k,label=s,value=v) for k,s,v in zip(keys,["A","B"],visible)]
    if defect=="missing":
        rows.pop()
    if defect=="duplicate":
        rows+=rows[:1]
    section="W12X40" if defect!="section" else "W12X45"
    rows.append(f"<h4 id='m-column'>column &mdash; {section} (column)</h4>")
    path=tmp_path/"report.html"
    path.write_text("".join(rows),encoding="utf-8")
    check=check_report_selections(path,calc)
    assert check["connection_component_rows_checked"]==2
    assert check["member_sections_checked"]==1
    assert check["passed"]==(defect=="none"),check
