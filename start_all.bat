@echo off
REM Start the local sample OData service in a new window
start "Sample OData Service" cmd /k "cd sample_odata_service && python -m venv .venv && .venv\Scripts\activate && pip install -r requirements.txt && uvicorn app:app --port 5000 --reload"

REM Wait a moment for the sample service to start
timeout /t 5 /nobreak >nul

REM Start the backend in a new window
start "OData Orchestration Backend" cmd /k "cd backend && python -m venv .venv && .venv\Scripts\activate && pip install -r requirements.txt && python run.py"

REM Wait for backend to start
timeout /t 5 /nobreak >nul

REM Seed sample services
start "Seed Services" cmd /k "cd backend && .venv\Scripts\activate && python -m scripts.seed_sample_service"

REM Start the frontend
start "Frontend" cmd /k "cd frontend && python -m http.server 3000"

echo.
echo All services starting. Once everything is up:
echo  - Backend:     http://localhost:8000
echo  - Frontend:    http://localhost:3000
echo  - Sample OData: http://localhost:5000
echo.
pause
