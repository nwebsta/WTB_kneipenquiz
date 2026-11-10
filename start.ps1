# Startet Bot und Admin-Seite (Windows). Legt beim ersten Mal das venv an.
# Bequemer: start.cmd doppelklicken.
Set-Location $PSScriptRoot

function Stop-WithMessage($msg) {
    Write-Host ""
    Write-Host $msg -ForegroundColor Red
    Read-Host "Enter druecken zum Schliessen"
    exit 1
}

if (-not (Test-Path .venv)) {
    Write-Host "Lege venv an und installiere Pakete ..."
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { Stop-WithMessage "Python nicht gefunden. Python 3.11+ installieren." }
    .\.venv\Scripts\python.exe -m pip install --upgrade pip
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Stop-WithMessage "Installation der Pakete fehlgeschlagen." }
}
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

