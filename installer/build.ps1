param(
    [string]$OutputRoot = (Join-Path $env:TEMP 'Borasuki-release-build')
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$buildRoot = [System.IO.Path]::GetFullPath($OutputRoot)
$userRoot = [System.IO.Path]::GetFullPath([Environment]::GetFolderPath('UserProfile'))
if ($buildRoot -eq $projectRoot -or $buildRoot -eq $userRoot -or
    $buildRoot -eq [System.IO.Path]::GetPathRoot($buildRoot)) {
    throw 'Choose a dedicated output directory, not the project, user profile or drive root.'
}
New-Item -ItemType Directory -Force -Path $buildRoot | Out-Null

$webview = Join-Path $buildRoot 'MicrosoftEdgeWebview2Setup.exe'
$expected = '81C01751C8CC385A5991ABB104205D42AC70094350EE8FB9E8EA580B51BB9554'
if (-not (Test-Path -LiteralPath $webview) -or (Get-FileHash -LiteralPath $webview -Algorithm SHA256).Hash -ne $expected) {
    Invoke-WebRequest 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' -OutFile $webview
}
$signature = Get-AuthenticodeSignature -LiteralPath $webview
if ((Get-FileHash -LiteralPath $webview -Algorithm SHA256).Hash -ne $expected -or
    $signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'CN=Microsoft Corporation') {
    throw 'The Microsoft WebView2 bootstrapper changed or failed signature validation; review and re-pin it.'
}

$compiler = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if ($compiler) { $iscc = $compiler.Source }
else { $iscc = Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe' }
if (-not (Test-Path -LiteralPath $iscc)) { throw 'Inno Setup 6 compiler is required.' }

Push-Location $projectRoot
try {
    & py -3.13 -m PyInstaller --noconfirm --clean --distpath (Join-Path $buildRoot 'dist') `
        --workpath (Join-Path $buildRoot 'work') Borasuki.spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }
    $appFolder = Join-Path $buildRoot 'dist\Borasuki'
    if (-not (Test-Path -LiteralPath (Join-Path $appFolder 'Borasuki.exe'))) { throw 'Frozen app is missing.' }
    & py -3.13 -I 'installer\check_bundle.py' $appFolder
    if ($LASTEXITCODE -ne 0) { throw 'External renderer imports failed; installer will not be built.' }
    & $iscc '/Qp' "/DBuildRoot=$appFolder" "/DWebView2Bootstrapper=$webview" `
        "/O$(Join-Path $buildRoot 'installer')" 'installer\Borasuki.iss'
    if ($LASTEXITCODE -ne 0) { throw 'Inno Setup compile failed.' }
    Get-FileHash -Algorithm SHA256 (Join-Path $buildRoot 'installer\Borasuki-Setup-1.0.0-beta.2-win64.exe')
}
finally {
    Pop-Location
}
