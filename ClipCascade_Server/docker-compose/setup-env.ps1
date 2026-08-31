# ClipCascade server .env setup - interactive, never prints secret values.
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File setup-env.ps1
# You will be prompted for each value; input is hidden for secrets.

$ErrorActionPreference = 'Stop'

$lib = 'C:\Users\JAY\.config\opencode\skills\secret-setup\scripts\secret-lib.ps1'
if (-not (Test-Path $lib)) { Write-Host "secret-lib.ps1 not found at $lib" -ForegroundColor Red; exit 1 }
. $lib

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$examplePath = Join-Path $here '.env.example'
$envPath = Join-Path $here '.env'

if (-not (Test-Path $examplePath)) { Write-Host '.env.example missing' -ForegroundColor Red; exit 1 }

Write-Step 'Preparing .env'
if (-not (Test-Path $envPath)) {
    Copy-Item $examplePath $envPath
    Write-Ok 'created .env from .env.example'
} else {
    Write-Ok '.env already exists (values will be merged/updated)'
}

if (Test-EnvIgnored -EnvPath $envPath) { Write-Ok '.env is gitignored' } else { Write-Fail '.env is NOT gitignored - aborting'; exit 1 }

function Test-NoDefault([string]$value, [string]$label) {
    if ([string]::IsNullOrWhiteSpace($value)) { Write-Fail "$label is empty"; return $false }
    $weak = @('admin123', 'password', '12345678', 'changeme', 'clipcascade')
    foreach ($w in $weak) { if ($value -eq $w) { Write-Fail "$label must not be a well-known default ($w)"; return $false } }
    if ($value.Length -lt 8) { Write-Fail "$label must be at least 8 characters"; return $false }
    return $true
}

# --- 1. non-secret config first ---
$adminUser = Read-Host 'Admin username (created only if the user database is empty)'
if ([string]::IsNullOrWhiteSpace($adminUser)) { Write-Fail 'Admin username is empty'; exit 1 }

$origins = Read-Host 'Allowed origins, comma-separated (e.g. http://localhost:8080 or https://clipcascade.example.com)'
if ([string]::IsNullOrWhiteSpace($origins)) { Write-Fail 'Allowed origins is empty'; exit 1 }
if ($origins -match '\*') { Write-Fail 'Wildcards are refused by the server in production'; exit 1 }
foreach ($o in $origins.Split(',')) {
    $t = $o.Trim()
    if ($t -and $t -notmatch '^https?://[A-Za-z0-9.\-\[\]:]+$') { Write-Fail "Origin '$t' is not a valid http(s) origin"; exit 1 }
}

# --- 2. secrets (hidden prompts) ---
$adminPass = Read-SecretPrompt -Label 'Admin password (min 8 chars, not a default)'
if (-not (Test-NoDefault $adminPass 'Admin password')) { exit 1 }

Write-Host 'Database password, H2 format: <file password> <user password> (two secrets, space-separated)' -ForegroundColor Gray
$dbPass = Read-SecretPrompt -Label 'Database password'
if (-not (Test-NoDefault $dbPass 'Database password')) { exit 1 }
$dbParts = $dbPass -split ' '
if ($dbParts.Count -ne 2) { Write-Fail 'H2 format needs exactly two space-separated secrets'; exit 1 }
foreach ($p in $dbParts) { if ($p.Length -lt 8) { Write-Fail 'Each of the two database secrets must be at least 8 characters'; exit 1 } }

# --- 3. write (lib handles backup, UTF-8 no BOM, read-back verify, rollback) ---
Write-Step 'Writing .env'
$r = Set-EnvSecret -EnvPath $envPath -Pairs @{
    CC_ADMIN_USERNAME  = $adminUser
    CC_ADMIN_PASSWORD  = $adminPass
    CC_ALLOWED_ORIGINS = $origins
    CC_SERVER_DB_PASSWORD = $dbPass
}
if (-not $r.Ok) { Write-Fail "write failed: $($r.Error)"; exit 1 }
Write-Ok 'values written and read back verified'

Write-Step 'Summary (values masked)'
Write-Host ("    CC_ADMIN_USERNAME  = " + $adminUser)
Write-Host ("    CC_ADMIN_PASSWORD  = " + (Format-Masked $adminPass))
Write-Host ("    CC_SERVER_DB_PASSWORD = " + (Format-Masked $dbPass))
Write-Host ("    CC_ALLOWED_ORIGINS = " + $origins)

Write-Host ''
Write-Host 'NOTE: real validation happens when the server starts - ProductionConfigValidator refuses to start on missing/invalid values.' -ForegroundColor Yellow
Write-Host 'Next: cd to this folder and run: docker compose up -d' -ForegroundColor Yellow
