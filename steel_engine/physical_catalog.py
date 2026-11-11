"""Record the selected HR profile from the same catalogue used by the engine."""
from . import sections


def selected_profile(model,name,*,id=None):
    # Do not derive plate geometry from A/I/J for custom equivalent sections.
    properties=sections.props(name)
    if not all(properties.get(k) for k in ('d','bf','tf','tw')):
        raise ValueError('Selected section requires explicit constituent geometry: '+name)
    origin=sections._csv_path()
    from .bim_json import file_digest
    from pathlib import Path
    evidence_path = Path(origin) if origin else Path(sections.__file__)
    source=dict(kind='engine_section_catalog',name=name,source=origin or 'sections._GEOM',
                source_sha256=file_digest(evidence_path),length_unit='in',
                selected_dimensions={k:properties[k] for k in ('d','bf','tf','tw')},
                geometric_idealization='Nominal straight plates; rolled root fillets not represented')
    return model.profile(id or name,'i',{k:properties[k] for k in ('d','bf','tf','tw')},source=source)
