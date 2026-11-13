#!/bin/sh
# Discord Bridge entrypoint.
# HA add-ons receive their options at /data/options.json; bridge.py reads it.
exec python3 /app/bridge.py
