"""Export-only command. Does not start the web server or run design analysis."""
from steel_engine.bim_export import main

if __name__ == "__main__":
    raise SystemExit(main())
