@echo off
echo Starting Parcel Manager...
if not exist data mkdir data
if not exist uploads\parcels mkdir uploads\parcels
if not exist .env copy .env.example .env
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
