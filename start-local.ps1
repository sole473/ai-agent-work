param(
    [switch]$AceOnly
)

$ErrorActionPreference = 'Stop'
$projectDir = $PSScriptRoot
$aceDir = Join-Path $projectDir 'models\ACE-Step-1.5'
$aceApi = Join-Path $aceDir '.venv\Scripts\acestep-api.exe'

if ($AceOnly) {
    Set-Location $aceDir
    $env:ACESTEP_CONFIG_PATH = 'acestep-v15-turbo'
    $env:ACESTEP_OFFLOAD_TO_CPU = 'true'
    $env:ACESTEP_INIT_LLM = 'false'

    $envFile = Join-Path $projectDir '.env.local'
    if (Test-Path $envFile) {
        $apiKeyLine = Get-Content $envFile | Where-Object { $_ -match '^ACESTEP_API_KEY=' } | Select-Object -First 1
        if ($apiKeyLine) {
            $env:ACESTEP_API_KEY = $apiKeyLine.Substring('ACESTEP_API_KEY='.Length).Trim().Trim('"').Trim("'")
        }
    }

    Write-Host 'Starting ACE-Step API. The first run may download model files.'
    & $aceApi --host 0.0.0.0 --port 8001
    exit $LASTEXITCODE
}

Set-Location $projectDir

if (-not (Test-Path '.env.local')) {
    throw 'Missing .env.local. Create it using the first-start instructions in README.md.'
}
if (-not (Test-Path 'models\BAAI-bge-small-zh-v1.5\config.json')) {
    throw 'Missing BGE model. Download it to models/BAAI-bge-small-zh-v1.5 as described in README.md.'
}
if (-not (Test-Path $aceApi)) {
    throw 'ACE-Step is not installed. Complete the first-time setup in README.md.'
}

docker version | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Docker Desktop is not ready. Start Docker Desktop and try again.'
}

docker compose --env-file .env.local -p music-agent-local config --quiet
if ($LASTEXITCODE -ne 0) {
    throw 'Compose configuration check failed. Check .env.local and compose.yaml.'
}

docker compose --env-file .env.local -p music-agent-local up -d --build
if ($LASTEXITCODE -ne 0) {
    throw 'Music Agent startup failed. Check the Compose logs for details.'
}

$powershell = (Get-Process -Id $PID).Path
$scriptPath = $PSCommandPath
$argumentLine = '-NoExit -ExecutionPolicy Bypass -File "' + $scriptPath + '" -AceOnly'
Start-Process -FilePath $powershell -ArgumentList $argumentLine

docker compose --env-file .env.local -p music-agent-local ps
Write-Host ''
Write-Host 'Music Agent is running in the background. ACE-Step started in a new PowerShell window.'
Write-Host 'Workspace: http://127.0.0.1:8081/'
Write-Host 'API docs: http://127.0.0.1:8081/docs'
Write-Host 'ACE-Step health: http://127.0.0.1:8001/health'
