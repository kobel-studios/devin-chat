$keyFile = Join-Path $PSScriptRoot "devin_api_key.txt"
if (Test-Path $keyFile) {
    $env:DEVIN_API_KEY = (Get-Content $keyFile -Raw).Trim()
}
Set-Location $PSScriptRoot
python app.py
