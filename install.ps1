# Installs parityrun on Windows, with nothing else needed first. In PowerShell:
#
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/arttujan/parityrun/main/install.ps1 | iex"
#
# uv (https://docs.astral.sh/uv/) installs parityrun as a command of its own, with its own Python when none fits.
# Run it again to update to the version below. $env:PARITYRUN_SOURCE installs from elsewhere: a folder, git+https://...
& {
    $ErrorActionPreference = "Stop"
    $version = "0.6.0"
    $source = if ($env:PARITYRUN_SOURCE) { $env:PARITYRUN_SOURCE } else { "parityrun==$version" }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Host "Installing uv, which installs parityrun ..."
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
        $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    }
    uv tool install --force $source
    if ($LASTEXITCODE -ne 0) { throw "Installing parityrun failed (see above)." }
    uv tool update-shell  # its PATH for new windows (no redirect: PowerShell 5.1 would stop on uv's stderr)
    $env:Path = "$(uv tool dir --bin);$env:Path"
    parityrun --version
    Write-Host ""
    Write-Host "Done. In your app's folder, run:  parityrun"
    Write-Host "(In another terminal window that was already open, open a new one first.)"
}
