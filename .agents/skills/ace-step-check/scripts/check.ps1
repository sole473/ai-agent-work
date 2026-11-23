param(
    [string]$TaskId,
    [string]$BaseUrl = 'http://127.0.0.1:8001'
)

$ErrorActionPreference = 'Continue'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..\..')).Path
$BaseUrl = $BaseUrl.TrimEnd('/')

Write-Output '=== ACE-Step local API ==='
$uri = [Uri]$BaseUrl
$tcp = Test-NetConnection -ComputerName $uri.Host -Port $uri.Port -WarningAction SilentlyContinue
Write-Output ("TCP {0}:{1}: {2}" -f $uri.Host, $uri.Port, $tcp.TcpTestSucceeded)

$headers = @{}
if ($env:ACESTEP_API_KEY) {
    $headers.Authorization = "Bearer $env:ACESTEP_API_KEY"
}
try {
    $health = Invoke-RestMethod -Uri "$BaseUrl/health" -Headers $headers -TimeoutSec 8
    Write-Output 'Health endpoint: responding'
    if ($null -ne $health) { $health | ConvertTo-Json -Depth 5 }
} catch {
    Write-Output ("Health endpoint: FAILED - {0}" -f $_.Exception.Message)
}

if ($TaskId) {
    Write-Output '=== ACE-Step task status ==='
    try {
        $payload = @{ task_id_list = @($TaskId) } | ConvertTo-Json -Compress
        $result = Invoke-RestMethod -Method Post -Uri "$BaseUrl/query_result" -Headers $headers -ContentType 'application/json' -Body $payload -TimeoutSec 15
        $result | ConvertTo-Json -Depth 8
    } catch {
        Write-Output ("Task query: FAILED - {0}" -f $_.Exception.Message)
    }
} else {
    Write-Output 'No TaskId supplied; skipped ACE-Step task query.'
}

Write-Output '=== Docker Compose services ==='
Push-Location $projectRoot
try {
    docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Output 'Docker daemon unavailable or access denied; cannot verify worker-side delivery.'
    } else {
        docker compose --env-file .env.local -p music-agent-local ps
        Write-Output '=== Last 100 worker log lines ==='
        docker compose --env-file .env.local -p music-agent-local logs --tail 100 worker
    }
} finally {
    Pop-Location
}

Write-Output '=== Diagnosis note ==='
Write-Output "Health alone does not prove task delivery; inspect worker logs and a real ACE-Step task ID."
