# Startet Bot und Admin-Seite (Windows). Legt beim ersten Mal das venv an.
# Bequemer: start.cmd doppelklicken.
Set-Location $PSScriptRoot

function Stop-WithMessage($msg) {
    Write-Host ""
    Write-Host $msg -ForegroundColor Red
    Read-Host "Enter druecken zum Schliessen"
    exit 1
}

function Find-Python {
    # Liefert einen Python-Aufruf mit Version 3.11 oder neuer, sonst $null.
    foreach ($cand in @('py -3.12', 'py -3.13', 'py -3.11', 'python')) {
        $parts = $cand.Split(' ')
        try {
            & $parts[0] $parts[1..($parts.Length - 1)] -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) { return $parts }
        } catch {}
    }
    return $null
}

if (-not (Test-Path .venv)) {
    $py = Find-Python
    if (-not $py) {
        Write-Host "Python 3.11+ nicht gefunden. Installiere Python 3.12 ueber winget ..."
        winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
        $py = Find-Python
        if (-not $py) { Stop-WithMessage "Python konnte nicht installiert werden. Bitte Python 3.12 von python.org installieren und neu starten." }
    }
    Write-Host "Lege venv an ..."
    & $py[0] $py[1..($py.Length - 1)] -m venv .venv
    if ($LASTEXITCODE -ne 0) { Stop-WithMessage "venv konnte nicht angelegt werden." }
    .\.venv\Scripts\python.exe -m pip install --upgrade pip
}
# Pakete bei jedem Start abgleichen (schnell, wenn schon alles da ist)
.\.venv\Scripts\python.exe -m pip install -q -r requirements.txt
if ($LASTEXITCODE -ne 0) { Stop-WithMessage "Installation der Pakete fehlgeschlagen." }
if (-not (Test-Path .env)) {
    Stop-WithMessage "Bitte zuerst .env.example nach .env kopieren, ausfuellen und speichern."
}

# Bei Absturz automatisch neu starten
while ($true) {
    .\.venv\Scripts\python.exe -m kneipenmonopoly
    if ($LASTEXITCODE -eq 1) { Stop-WithMessage "Konfigurationsfehler, siehe Meldungen oben. Bitte .env pruefen." }
    Write-Host "Prozess beendet (Code $LASTEXITCODE), Neustart in 3 Sekunden ... (Strg+C zum Abbrechen)"
    Start-Sleep -Seconds 3
}

