#!/bin/bash
echo "Starting Parcel Manager..."
mkdir -p data uploads/parcels
[ -f .env ] || cp .env.example .env
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
