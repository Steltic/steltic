"""Record the selected HR profile from the same catalogue used by the engine."""
from . import sections


def selected_profile(model,name,*,id=None):
    # Do not derive plate geometry from A/I/J for custom equivalent sections.
    properties=sections.props(name)
    if not all(properties.get(k) for k in ('d','bf','tf','tw')):
        raise ValueError('Selected section requires explicit constituent geometry: '+name)
    origin=sections._csv_path()
    source=dict(kind='engine_section_catalog',name=name,source=origin or 'sections._GEOM',
                geometric_idealization='Nominal straight plates; rolled root fillets not represented')
    return model.profile(id or name,'i',{k:properties[k] for k in ('d','bf','tf','tw')},source=source)
