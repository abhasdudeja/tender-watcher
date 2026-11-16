<#
.SYNOPSIS
  Crawl some sources from THIS machine (an Indian IP) and publish the result to the repo.

.DESCRIPTION
  For portals that refuse GitHub's cloud IPs (Gujarat, the Mizoram notice board, Andhra Pradesh, Chhattisgarh, and
  the two GeM CPPP lists, about an hour for the central one). Those are marked enabled: false in sources.yaml, so the GitHub crawl skips them; a source named here runs even when
  disabled. Steps:
    1. refuse to start unless the working tree is clean, we are on master and nothing is unpushed;
       fast-forward to origin/master (never a merge, never a force);
    2. crawl the given sources (--no-index), then rebuild docs/data from data/;
    3. commit data/ and docs/data/ (nothing changed = nothing committed);
    4. push (plain, no force). If origin moved meanwhile (the Actions crawl), our own commit is replaced by the
       same sources' data on top of the new origin, the index is rebuilt, and the push is retried.
  Everything printed is meant to be read by a person (or Claude): the last line says what happened.

  Run:   powershell -NoProfile -ExecutionPolicy Bypass -File tools\local_crawl.ps1
         ... -Slugs gujarat,mizoram-etender,andhrapradesh,chhattisgarh,gemcppp,gemcppp-state   (the default)
         ... -NoPush                          (commit locally only)
  Logs:  logs\local-crawl-<timestamp>.log (the crawler's own log)
#>
param(
  [string[]]$Slugs = @("gujarat", "mizoram-etender", "andhrapradesh", "chhattisgarh", "gemcppp", "gemcppp-state"),   # the sources set to enabled: false in sources.yaml
  [switch]$NoPush,
  [int]$MaxPushTries = 3
)

$ErrorActionPreference = "Continue"      # native commands report through exit codes, checked below
Set-Location (Split-Path -Parent $PSScriptRoot)
$env:GIT_TERMINAL_PROMPT = "0"           # never wait for a password prompt
$env:GCM_INTERACTIVE = "never"
$py = Join-Path (Get-Location) ".venv\Scripts\python.exe"
$remote = "origin/master"
$gitLog = "logs\git-sync.log"

function Abort($msg) { Write-Output "ABORT: $msg"; exit 1 }
function Run-Index {
  & $py -X utf8 -m crawler.run --index-only | Out-Null
  if ($LASTEXITCODE -ne 0) { Abort "rebuilding the site index failed (exit $LASTEXITCODE)" }
}
function Git-Logged {
  # Run git with stderr merged, append everything to $gitLog as UTF-8 (Tee-Object writes UTF-16 on Windows PowerShell 5.1,
  # which garbled the log), echo it, and leave the result in $script:GitCode / $script:GitOut.
  param([string[]]$GitArgs)
  $lines = @(& git @GitArgs 2>&1 | ForEach-Object { "$_" })      # "$_" turns stderr ErrorRecords into plain text
  $script:GitCode = $LASTEXITCODE
  $script:GitOut = $lines -join "`n"
  if ($lines) { $lines | Add-Content -Path $gitLog -Encoding UTF8; $lines | Out-Host }
}
function Last-GitError { (($script:GitOut -split "`n") | Where-Object { $_ -match "error:|fatal:|rejected|denied|failed|Could not|unable" } | Select-Object -Last 3) -join " | " }
function Active-Total {
  $m = Get-Content docs\data\index.json -Raw -Encoding UTF8 | ConvertFrom-Json
  ($m.sources | Measure-Object -Property active -Sum).Sum
}
function Commit-Data {
  git add data docs/data
  if (-not (git status --porcelain)) { return $false }
  $msg = "crawl (local): $($Slugs -join ', ') - $(Active-Total) active tenders"
  git commit -q -m $msg
  if ($LASTEXITCODE -ne 0) { Abort "git commit failed" }
  return $true
}

# ---- 1. preconditions
if (-not (Test-Path $py)) { Abort "no virtualenv at $py" }
if (Test-Path data\.crawl.lock) { Abort "data\.crawl.lock exists: a crawl is running here (or died and left its lock)" }
if ((git rev-parse --abbrev-ref HEAD) -ne "master") { Abort "not on master" }
$dirty = git status --porcelain
if ($dirty) { Abort "working tree is not clean:`n$($dirty -join "`n")" }
New-Item -ItemType Directory -Force logs | Out-Null
Write-Output "git fetch origin (verbose, logging to $gitLog)"
"=== $(Get-Date -Format s) git fetch origin ===" | Add-Content $gitLog -Encoding UTF8
Git-Logged @("fetch","origin","-v","--progress")
if ($script:GitCode -ne 0) { Abort "git fetch failed (offline? not logged in? see $gitLog): $(Last-GitError)" }
Write-Output "git fetch done: origin/master = $(git rev-parse --short origin/master)"
if ([int](git rev-list --count "$remote..HEAD") -ne 0) { Abort "there are local commits that are not pushed yet" }
Git-Logged @("merge","--ff-only",$remote)
if ($script:GitCode -ne 0) { Abort "could not fast-forward to $remote (see $gitLog): $(Last-GitError)" }

# ---- 2. crawl, then rebuild the index
New-Item -ItemType Directory -Force logs | Out-Null
$log = "logs\local-crawl-$(Get-Date -Format 'yyyyMMdd-HHmm').log"
$crawlArgs = @("-X", "utf8", "-m", "crawler.run", "--no-index")
foreach ($s in $Slugs) { $crawlArgs += @("--source", $s) }
Write-Output "crawling $($Slugs -join ', ') (log: $log)"
$p = Start-Process -FilePath $py -ArgumentList $crawlArgs -NoNewWindow -Wait -PassThru -RedirectStandardError $log -RedirectStandardOutput "$log.out"
if ($p.ExitCode -eq 2) { Abort "another crawl holds the data directory" }
Write-Output "crawler exit code $($p.ExitCode) (1 = a source reported problems; its last good data is kept)"
Run-Index

# per-source result, for the report
foreach ($s in $Slugs) {
  $st = Get-Content "data\$s\_status.json" -Raw -Encoding UTF8 | ConvertFrom-Json
  Write-Output ("  {0}: ok={1} listed={2} new={3} details={4} errors={5}" -f $s, $st.ok, $st.listed, $st.new, $st.details_fetched, @($st.errors).Count)
}

# ---- 3. commit
if (-not (Commit-Data)) { Write-Output "DONE: nothing changed, nothing to publish"; exit 0 }
if ($NoPush) { Write-Output "DONE: committed locally (-NoPush)"; exit 0 }

# ---- 4. push, re-applying our sources on top of a newer origin when needed
for ($try = 1; $try -le $MaxPushTries; $try++) {
  "=== $(Get-Date -Format s) git push origin master (try $try) ===" | Add-Content $gitLog -Encoding UTF8
  Git-Logged @("push","origin","master","-v","--progress")
  if ($script:GitCode -eq 0) { Write-Output "DONE: pushed ($(git log -1 --format='%h %s'))"; exit 0 }
  # Only a non-fast-forward rejection means "origin moved"; auth, network and permission errors must not be retried as such.
  if ($script:GitOut -notmatch "non-fast-forward|fetch first|\[rejected\]|stale info") {
    Abort "git push failed (our commit is still local; see $gitLog): $(Last-GitError)"
  }
  Write-Output "push attempt $try was rejected (non-fast-forward); origin has moved, re-applying our data on top"
  Git-Logged @("fetch","origin","-v","--progress")
  if ($script:GitCode -ne 0) { Abort "git fetch failed (see $gitLog): $(Last-GitError)" }
  if ([int](git rev-list --count "$remote..HEAD") -ne 1 -or -not ((git log -1 --format=%s) -like "crawl (local)*")) {
    Abort "unexpected local history; not touching it"
  }
  $keep = Join-Path $env:TEMP "tenderwatch-keep-$PID"
  Remove-Item $keep -Recurse -Force -ErrorAction SilentlyContinue
  New-Item -ItemType Directory -Force $keep | Out-Null
  foreach ($s in $Slugs) { Copy-Item "data\$s" (Join-Path $keep $s) -Recurse }
  git reset -q --hard $remote                   # drops only our own unpushed commit (checked above)
  foreach ($s in $Slugs) {
    $theirs = $null
    if (Test-Path "data\$s\_status.json") { $theirs = (Get-Content "data\$s\_status.json" -Raw -Encoding UTF8 | ConvertFrom-Json).finished }
    $ours = (Get-Content (Join-Path $keep "$s\_status.json") -Raw -Encoding UTF8 | ConvertFrom-Json).finished
    if ($theirs -and $theirs -gt $ours) { Write-Output "  $s : origin's data is newer than ours, kept origin's"; continue }
    if (Test-Path "data\$s") { Remove-Item "data\$s" -Recurse -Force }
    Copy-Item (Join-Path $keep $s) "data\$s" -Recurse
  }
  Remove-Item $keep -Recurse -Force -ErrorAction SilentlyContinue
  Run-Index
  if (-not (Commit-Data)) { Write-Output "DONE: origin already has everything"; exit 0 }
}
Abort "could not push after $MaxPushTries attempts (our commit is still local)"
