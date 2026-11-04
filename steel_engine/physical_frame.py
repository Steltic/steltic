"""Map an elastic element's section axes to the BIM profile width direction.

HR's add_column uses (Iy_weak, Iz_strong), whereas add_beam uses
(Iy_strong, Iz_weak). The analysis local-y vector is therefore not invariably
the flange-width vector. Recover this from the recorded inertia ratio, never
from a member's name or an assumed global direction. Unknown mappings remain
unresolved; this does not rotate or modify the analysis element.
"""
import math


def width_axis(axis,analysis_y,arguments,properties):
    if not analysis_y or len(arguments)<11 or arguments[0]!='elasticBeamColumn':return None
    values=[arguments[8],arguments[9],properties.get('Ix'),properties.get('Iy')]
    if any(type(v) not in (float,int) or not math.isfinite(v) or v<=0 for v in values):return None
    iy,iz,strong,weak=values;ratio=iy/iz;section_ratio=strong/weak
    if math.isclose(section_ratio,1,rel_tol=.001):
        return list(analysis_y) if math.isclose(ratio,1,rel_tol=.001) else None
    # Source stiffness values can be rounded or modified. Their principal-axis
    # ordering still establishes which vector follows the section width. A
    # nearly equal pair cannot establish a distinct strong/weak orientation.
    if math.isclose(ratio,1,rel_tol=.05):return None
    if (ratio>1)==(section_ratio>1):return list(analysis_y)
    u=[b-a for a,b in zip(*axis)];length=math.sqrt(sum(v*v for v in u))
    if not length:return None
    u=[v/length for v in u];v=analysis_y
    return [u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]]
