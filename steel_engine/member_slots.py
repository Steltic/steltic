"""Explicit rectangular slot envelopes in a member's local section coordinates.

Slots are selected inputs, never inferred from an intersecting workline. Their
straight ends are design-review geometry; machining radii remain separate data.
"""
import math


def validated(slots,length):
    if not isinstance(slots,list) or any(not isinstance(slot,dict) for slot in slots):raise ValueError('Slots must be a list of explicit selections')
    for slot in slots:
        if slot.get('section_axis') not in (0,1):raise ValueError('A slot needs local section axis 0 or 1')
        values=[slot.get(k) for k in ('start_in','end_in','width_in')]
        if not all(type(v) in (int,float) and math.isfinite(v) for v in values):raise ValueError('Slot dimensions must be finite numbers')
        lo,hi,width=values
        if not 0<=lo<hi<=length+1e-7 or width<=0:raise ValueError('Slot must lie within the physical member length')
        centre=slot.get('centre_in',0)
        if type(centre) not in (int,float) or not math.isfinite(centre):raise ValueError('Invalid slot centre')
    return slots


def half_plane(poly,axis,limit,side):
    result=[]
    for a,b in zip(poly,poly[1:]+poly[:1]):
        da=side*(a[axis]-limit);db=side*(b[axis]-limit)
        if da>=-1e-10:result.append(a)
        if da*db < -1e-18:
            fraction=da/(da-db);result.append([x+fraction*(y-x) for x,y in zip(a,b)])
    area=sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(result,result[1:]+result[:1])) if result else 0
    return result if len(result)>=3 and abs(area)>1e-12 else []


def cut_sections(polys,slots,station):
    for slot in slots:
        if not slot['start_in']<station<slot['end_in']:continue
        centre=slot.get('centre_in',0);half=slot['width_in']/2
        polys=[clipped for poly in polys for clipped in (
            half_plane(poly,slot['section_axis'],centre-half,-1),
            half_plane(poly,slot['section_axis'],centre+half,1)) if clipped]
    return polys
