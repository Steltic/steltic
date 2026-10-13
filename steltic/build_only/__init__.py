"""Build-only mode: model a building the USER designed, exactly as given, for the nonlinear suite.

HR never redesigns here and never invents a value. The flow is

    user files (template .xlsx / ETABS .e2k / JSON)
        -> intake            building_description.json  (+ the items the file did not settle)
        -> validate          missing_items.json: KEY items (questions) and DEFAULT proposals (need acceptance)
        -> answers           the user's answers / accepted defaults (answers.json)
        -> translate         cfg.py: the user's members one for one (custom_build), nothing sized
        -> build             the normal HR pipeline -> model_opensees.py, model_static.py, report.html,
                             design/*; provenance.json (the source of every cfg value)

Modules: schema (description format, validation, answers), translate (description -> cfg.py text),
runner (build a job), intake_xlsx / intake_e2k (files -> description), extract (an HR job -> description,
used to re-enter HR test buildings as user designs), cli (python -m steltic.build_only ...).
"""
SCHEMA = "steltic.building_description"
VERSION = 1
