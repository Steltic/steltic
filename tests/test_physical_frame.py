import copy
import pytest
from steel_engine.physical_frame import width_axis


@pytest.mark.parametrize('axis,y,inertias,expected',[
    ([[0,0,0],[0,0,144]],[1,0,0],[2560,7190],[0,1,0]),
    ([[0,0,0],[0,0,144]],[0,-1,0],[2560,7190],[1,0,0]),
    ([[0,0,0],[360,0,0]],[0,1,0],[7190,2560],[0,1,0]),
    ([[0,0,0],[0,360,0]],[-1,0,0],[7190,2560],[-1,0,0]),
])
def test_physical_flange_direction_follows_recorded_stiffness_axes(axis,y,inertias,expected):
    args=['elasticBeamColumn',1,1,2,134,29000,11200,395,*inertias,2]
    original=copy.deepcopy(args)
    assert width_axis(axis,y,args,dict(Ix=7190,Iy=2560))==expected
    assert args==original


def test_missing_or_ambiguous_inertias_require_explicit_orientation():
    args=['elasticBeamColumn',1,1,2,134,29000,11200,395,100,100,2]
    assert width_axis([[0,0,0],[0,0,144]],[1,0,0],args,dict(Ix=7190,Iy=2560)) is None
    assert width_axis([[0,0,0],[0,0,144]],[1,0,0],args,{}) is None


def test_saved_rounded_inertias_retain_their_original_principal_axes():
    args=['elasticBeamColumn',1,1,2,134,29000,11200,395,4060,10500,2]
    assert width_axis([[0,0,0],[0,0,144]],[1,0,0],args,dict(Ix=10800,Iy=3680))==[0,1,0]
