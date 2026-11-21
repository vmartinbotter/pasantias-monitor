# Crea la tarea "Monitor pasantias - Workday" en el Programador de tareas de Windows.
# Corre todos los días a las 10:00. Si a esa hora la compu estaba apagada,
# corre apenas la prendas (StartWhenAvailable).
# Uso: clic derecho en este archivo > "Ejecutar con PowerShell"
$carpeta = Split-Path -Parent $MyInvocation.MyCommand.Path
$bat = Join-Path $carpeta "correr_workday.bat"
$accion = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$bat`"" -WorkingDirectory $carpeta
$cuando = New-ScheduledTaskTrigger -Daily -At 10:00
$opciones = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "Monitor pasantias - Workday" -Action $accion -Trigger $cuando `
    -Settings $opciones -Description "Revisa pasantias en empresas de Workday" -Force | Out-Null
Write-Host "Listo: la tarea 'Monitor pasantias - Workday' corre todos los dias a las 10:00." -ForegroundColor Green
Read-Host "Apreta Enter para cerrar"
