import copy
from test_bim_selected_components import package
from steel_engine.bim_contract import connection_component_findings


def missing(p):
    return connection_component_findings(p)[0]['examples'][0]['missing_component_categories']


def test_anchor_bar_is_its_own_solid_component_not_an_extra_foundation_rod():
    p,c,_=package('Two 2 in ASTM A193 B7 rods end on an anchor bar PL 2.75 x 7 x 24 in.')
    assert missing(p)==['anchor_bar']
    bar=copy.deepcopy(c['parts'][0]);bar.update(id='bar',kind='plate',parameters=dict(description='Source far-flange anchor bar'))
    c['parts'].append(bar)
    assert not connection_component_findings(p)


def test_anchor_bar_note_does_not_replace_an_anchor_rod_or_a_solid():
    p,c,_=package('Four F1554 anchor rods with an anchor bar')
    bar=copy.deepcopy(c['parts'][0]);bar.update(id='bar',kind='plate',parameters=dict(description='Source anchor bar'))
    c['parts'].append(bar)
    assert missing(p)==['anchor']
    c['parts'][0]['kind']='anchor'
    assert not connection_component_findings(p)
    bar['geometry']=dict(type='polyline',points=[[0,0,0],[1,0,0]])
    assert missing(p)==['anchor_bar']


def test_generic_plate_with_anchor_count_cannot_impersonate_a_selected_bar():
    p,c,_=package('Anchor bar PL 3 x 8 x 24 in')
    c['parts'][0]['parameters']=dict(anchor_count=1)
    assert missing(p)==['anchor_bar']
