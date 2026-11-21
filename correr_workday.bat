@echo off
REM Revisa las empresas de Workday desde tu compu (GitHub está bloqueado por Workday).
REM Lo ejecuta solo el Programador de tareas de Windows; también podés hacerle doble clic.
cd /d "%~dp0"
set PYTHONUTF8=1
echo. >> estado\log_local.txt
echo ===== %date% %time% ===== >> estado\log_local.txt
git pull --quiet >> estado\log_local.txt 2>&1
python monitor.py --solo workday >> estado\log_local.txt 2>&1
git add estado/vistos.json
git diff --cached --quiet || git commit -m "estado workday (pc)" --quiet
git push --quiet >> estado\log_local.txt 2>&1
