<#
.SYNOPSIS
  What the "TenderWatch local crawl" scheduled task runs: tools\local_crawl.ps1, with retries, logged to logs\scheduler.log.

.DESCRIPTION
  Runs local_crawl.ps1 in its own PowerShell, puts a timestamp in front of every line it prints and appends the lines to
  logs\scheduler.log (the format tools\watch_crawl.ps1 reads). If the run stops for a reason that can clear up by itself
  (git fetch or push failed: network down, GitHub down, login not ready yet after a wake-up; or a manual crawl holding
  the data directory), it waits -WaitMinutes and runs again, at most -Retries more times. A crawl commit that could not be
  pushed is not lost: the next run of local_crawl.ps1 carries it over and publishes it.

  The exit code is local_crawl.ps1's last one, so the task's "Last Run Result" is 0 when the run published (or had
  nothing to publish) and 0x1 when it stopped; the last ABORT line in logs\scheduler.log says why.

  Run:   powershell -NoProfile -ExecutionPolicy Bypass -File tools\scheduled_crawl.ps1 [-Retries 2] [-WaitMinutes 30]
#>
param(
  [int]$Retries = 2,
  [int]$WaitMinutes = 30
)

Set-Location (Split-Path -Parent $PSScriptRoot)
New-Item -ItemType Directory -Force logs | Out-Null
$schedLog = "logs\scheduler.log"
$crawl = Join-Path $PSScriptRoot "local_crawl.ps1"
# ABORT reasons worth another try later; everything else (dirty tree, unexpected history, missing venv...) needs a person.
$retryable = "git fetch failed|git push failed|could not push after|another crawl holds the data directory"

function Write-Log([string]$line) {
  $stamped = "{0} {1}" -f (Get-Date -Format s), $line
  Add-Content -Path $schedLog -Value $stamped -Encoding UTF8
  $stamped
}

$code = 1
for ($run = 1; $run -le $Retries + 1; $run++) {
  if ($run -gt 1) {
    Write-Log "scheduled_crawl: trying again in $WaitMinutes minutes (retry $($run - 1) of $Retries)"
    Start-Sleep -Seconds ($WaitMinutes * 60)
  }
  $lines = @(& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $crawl 2>&1 | ForEach-Object { $l = "$_"; Write-Host (Write-Log $l); $l })   # Write-Host: shown when run by hand, not captured
  $code = $LASTEXITCODE
  if ($code -eq 0) { exit 0 }
  $last = $lines | Where-Object { $_ -match '^ABORT:' } | Select-Object -Last 1
  if (-not $last) { $last = "(no ABORT line; local_crawl.ps1 exited with code $code)" }
  if ($last -notmatch $retryable) {
    Write-Log "scheduled_crawl: not retrying, this needs a person: $last"
    exit $code
  }
}
Write-Log "scheduled_crawl: giving up after $($Retries + 1) runs"
exit $code
