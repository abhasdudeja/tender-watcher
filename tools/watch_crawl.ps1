<#
.SYNOPSIS
  Live, read-only view of a running local crawl (tools\local_crawl.ps1, or any crawler.run).

.DESCRIPTION
  Redraws every few seconds: whether a crawl holds data\.crawl.lock (and its process is alive), the progress of every
  source that is not finished, how many are done, the tail of the crawler's own log (DEBUG and progress lines left
  out) and the last lines of logs\scheduler.log (what the Windows task printed: DONE: / ABORT:). It changes nothing and
  can be closed (Ctrl+C) at any time; the crawl does not depend on it. When no crawl is running any more it draws a
  last time and exits.

  Run:   powershell -NoProfile -ExecutionPolicy Bypass -File tools\watch_crawl.ps1
         ... -Every 10        seconds between redraws (default 5)
         ... -Lines 12        crawler log lines shown (default 8)
         ... -Once            draw once and exit
#>
param(
  [int]$Every = 5,
  [int]$Lines = 8,
  [switch]$Once
)

Set-Location (Split-Path -Parent $PSScriptRoot)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$py = ".\.venv\Scripts\python.exe"

$script:buf = $null
function P([string]$line) { [void]$script:buf.Add($line) }    # lines are collected first and drawn at once, so the screen does not blank while the progress is read
function Cut([string]$s, [int]$n = 150) { if ($s.Length -gt $n) { return $s.Substring(0, $n) } else { return $s } }

# draws the view; returns $true while a crawl process is alive
function Show-Crawl {
  $lock = $null
  if (Test-Path "data\.crawl.lock") { try { $lock = Get-Content "data\.crawl.lock" -Raw | ConvertFrom-Json } catch { $lock = $null } }
  $alive = $false
  if ($lock) { $alive = [bool](Get-Process -Id $lock.pid -ErrorAction SilentlyContinue) }
  $state = if (-not $lock) { "no crawl is holding the lock" }
           elseif ($alive) { "RUNNING   pid $($lock.pid), started $($lock.started)" }
           else { "STALE LOCK   pid $($lock.pid) is gone (remove data\.crawl.lock before the next run)" }
  P ("TenderWatch crawl watch   {0}   (Ctrl+C closes this view; the crawl keeps running)" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
  P "state: $state"
  P ""

  $rows = @(& $py -X utf8 -m crawler.run --show-progress 2>&1 | ForEach-Object { "$_" } | Where-Object { $_ -match '^\[' })
  $open = @($rows | Where-Object { $_ -notmatch '\(done\)' })
  P ("sources: {0} done, {1} in progress" -f ($rows.Count - $open.Count), $open.Count)
  foreach ($r in $open) { P ("  " + (Cut $r)) }
  P ""

  $log = Get-ChildItem "logs\local-crawl-*.log" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime | Select-Object -Last 1
  if ($log) {
    P ("log: {0}  (last written {1}s ago)" -f $log.Name, [int]((Get-Date) - $log.LastWriteTime).TotalSeconds)
    Get-Content $log.FullName -Tail 300 -ErrorAction SilentlyContinue |
      Where-Object { $_ -notmatch ' DEBUG ' -and $_ -notmatch 'tenderwatch\.progress' } |
      Select-Object -Last $Lines | ForEach-Object { P ("  " + (Cut $_ 160)) }
  } else { P "log: no logs\local-crawl-*.log yet" }
  P ""

  if (Test-Path "logs\scheduler.log") {
    P "scheduler.log (last lines):"
    Get-Content "logs\scheduler.log" -Tail 4 -ErrorAction SilentlyContinue | ForEach-Object { P ("  " + (Cut $_ 160)) }
  }
  return $alive
}

while ($true) {
  $script:buf = New-Object System.Collections.Generic.List[string]
  $running = Show-Crawl
  if (-not $Once) { Clear-Host }
  foreach ($l in $script:buf) { Write-Host $l }
  if ($Once) { break }
  if (-not $running) { Write-Host ""; Write-Host "No crawl is running any more: done watching."; break }
  Start-Sleep -Seconds $Every
}
