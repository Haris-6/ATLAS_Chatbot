#!/bin/bash
# Azure App Service startup command for ATLAS.
# Azure auto-detects this when present in the project root.
# We bind to 0.0.0.0 so the platform's reverse proxy can reach us;
# PORT is injected by Azure App Service at runtime.
export HOST=0.0.0.0
python bot_server.py
