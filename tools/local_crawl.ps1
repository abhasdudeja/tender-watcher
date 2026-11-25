<#
.SYNOPSIS
  Crawl some sources from THIS machine (an Indian IP) and publish the result to the repo.

.DESCRIPTION
  For portals that refuse GitHub's cloud IPs (Gujarat, the Mizoram notice board, Andhra Pradesh, Chhattisgarh, and
  the two GeM CPPP lists). Those are marked enabled: false in sources.yaml, so the GitHub crawl skips them; a source named here runs even when
  disabled. Steps:
    1. refuse to start unless the working tree is clean and we are on master; fast-forward to origin/master (never a
       merge, never a force). If an earlier run committed but could not push (network, login), that one "crawl (local)"
       commit is not thrown away: its sources' data is carried on top of origin/master and published with this run;
       any other unpushed commit stops the run;
    2. crawl the given sources (--no-index), then rebuild docs/data from data/;
    3. report each source, record it in logs\local-crawl-history.csv and print a WARN line for a source that reported
       problems, found no new tenders in its last -StaleRuns runs, or lists far fewer tenders than in its recent runs;
    4. commit data/ and docs/data/ (nothing changed = nothing committed);
    5. push (plain, no force). If origin moved meanwhile (the Actions crawl), our own commit is replaced by the
       same sources' data on top of the new origin, the index is rebuilt, and the push is retried.
  Everything printed is meant to be read by a person (or Claude): the last line says what happened (DONE/ABORT, with
  the number of warnings). The scheduled task runs this through tools\scheduled_crawl.ps1, which retries a run that
  stopped on a git fetch/push failure.

  Run:   powershell -NoProfile -ExecutionPolicy Bypass -File tools\local_crawl.ps1
         ... -Slugs gujarat,mizoram-etender,andhrapradesh,chhattisgarh,gemcppp,gemcppp-state   (the default)
         ... -NoPush                          (commit locally only)
  Logs:  logs\local-crawl-<timestamp>.log (the crawler's own log), logs\local-crawl-history.csv (one row per source per run)
#>
param(
  [string[]]$Slugs = @("gujarat", "mizoram-etender", "andhrapradesh", "chhattisgarh", "gemcppp", "gemcppp-state"),   # the sources set to enabled: false in sources.yaml
  [switch]$NoPush,
  [int]$MaxPushTries = 3,
  [int]$StaleRuns = 3,            # warn when a source found no new tenders in this many runs in a row ...
  [int]$StaleMinListed = 50,      # ... and lists at least this many (a small board may well have nothing new)
  [double]$DropShare = 0.3        # warn when a source lists this share fewer tenders than the median of its last 5 runs
)

$ErrorActionPreference = "Continue"      # native commands report through exit codes, checked below
Set-Location (Split-Path -Parent $PSScriptRoot)
$env:GIT_TERMINAL_PROMPT = "0"           # never wait for a password prompt
$env:GCM_INTERACTIVE = "never"
$py = Join-Path (Get-Location) ".venv\Scripts\python.exe"
$remote = "origin/master"
$gitLog = "logs\git-sync.log"
$historyFile = "logs\local-crawl-history.csv"
$Published = @($Slugs)                   # the sources this run's commit carries (plus those of a carried-over commit)
$warnings = @()

function Abort($msg) { Write-Output "ABORT: $msg"; exit 1 }
function Done($msg) {
  if ($warnings.Count) { $msg += " - $($warnings.Count) warning(s), see the WARN lines above" }
  Write-Output "DONE: $msg"; exit 0
}
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
  $msg = "crawl (local): $($Published -join ', ') - $(Active-Total) active tenders"
  git commit -q -m $msg
  if ($LASTEXITCODE -ne 0) { Abort "git commit failed" }
  return $true
}
function Carry-OntoOrigin([string[]]$Keep) {
  # Put the working tree's data of the $Keep sources on top of the newest origin/master: copy it aside, reset to origin
  # (which drops only our own unpushed "crawl (local)" commit - the callers check that), and copy each source back unless
  # origin's copy is newer. The result is left uncommitted.
  $keepDir = Join-Path $env:TEMP "tenderwatch-keep-$PID"
  Remove-Item $keepDir -Recurse -Force -ErrorAction SilentlyContinue
  New-Item -ItemType Directory -Force $keepDir | Out-Null
  foreach ($s in $Keep) { if (Test-Path "data\$s") { Copy-Item "data\$s" (Join-Path $keepDir $s) -Recurse } }
  git reset -q --hard $remote
  foreach ($s in $Keep) {
    $mine = Join-Path $keepDir $s
    if (-not (Test-Path (Join-Path $mine "_status.json"))) { continue }
    $theirs = $null
    if (Test-Path "data\$s\_status.json") { $theirs = (Get-Content "data\$s\_status.json" -Raw -Encoding UTF8 | ConvertFrom-Json).finished }
    $ours = (Get-Content (Join-Path $mine "_status.json") -Raw -Encoding UTF8 | ConvertFrom-Json).finished
    if ($theirs -and $theirs -gt $ours) { Write-Output "  $s : origin's data is newer than ours, kept origin's"; continue }
    if (Test-Path "data\$s") { Remove-Item "data\$s" -Recurse -Force }
    Copy-Item $mine "data\$s" -Recurse
  }
  Remove-Item $keepDir -Recurse -Force -ErrorAction SilentlyContinue
}
function Median([double[]]$xs) {
  $xs = @($xs | Sort-Object); $n = $xs.Count
  if ($n -eq 0) { return $null }
  if ($n % 2) { return $xs[($n - 1) / 2] }
  return ($xs[$n / 2 - 1] + $xs[$n / 2]) / 2
}
function Warn($msg) { $script:warnings += $msg; Write-Output "WARN: $msg" }

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
$ahead = [int](git rev-list --count "$remote..HEAD")
if ($ahead -ne 0) {
  # Only one case is handled: a single "crawl (local)" commit an earlier run made but could not push.
  $subject = git log -1 --format=%s
  if ($ahead -ne 1 -or $subject -notmatch '^crawl \(local\): (.+) - \d+ active tenders$') {
    Abort "there are local commits that are not pushed yet"
  }
  $carried = @($Matches[1] -split ',\s*' | Where-Object { $_ })
  Write-Output "an earlier run's commit was never pushed ($(git log -1 --format='%h %cd' --date=format:'%d-%m-%Y %H:%M')); carrying its data ($($carried -join ', ')) on top of $remote"
  Carry-OntoOrigin $carried
  $Published = @($Slugs + $carried | Select-Object -Unique)
} else {
  Git-Logged @("merge","--ff-only",$remote)
  if ($script:GitCode -ne 0) { Abort "could not fast-forward to $remote (see $gitLog): $(Last-GitError)" }
}

# ---- 2. crawl, then rebuild the index
$log = "logs\local-crawl-$(Get-Date -Format 'yyyyMMdd-HHmm').log"
$crawlArgs = @("-X", "utf8", "-m", "crawler.run", "--no-index")
foreach ($s in $Slugs) { $crawlArgs += @("--source", $s) }
Write-Output "crawling $($Slugs -join ', ') (log: $log)"
$p = Start-Process -FilePath $py -ArgumentList $crawlArgs -NoNewWindow -Wait -PassThru -RedirectStandardError $log -RedirectStandardOutput "$log.out"
if ($p.ExitCode -eq 2) { Abort "another crawl holds the data directory" }
Write-Output "crawler exit code $($p.ExitCode) (1 = a source reported problems; its last good data is kept)"
Run-Index

# ---- 3. per-source result: report, record in the history, warn about sources that look stale
$history = @()
if (Test-Path $historyFile) { $history = @(Import-Csv $historyFile -Encoding UTF8) }
$now = Get-Date -Format s
foreach ($s in $Slugs) {
  $st = Get-Content "data\$s\_status.json" -Raw -Encoding UTF8 | ConvertFrom-Json
  $errs = @($st.errors)
  Write-Output ("  {0}: ok={1} listed={2} new={3} details={4} errors={5}" -f $s, $st.ok, $st.listed, $st.new, $st.details_fetched, $errs.Count)
  [pscustomobject]@{ time = $now; slug = $s; ok = $st.ok; listed = [int]$st.listed; new = [int]$st.new; errors = $errs.Count } |
    Export-Csv $historyFile -Append -NoTypeInformation -Encoding UTF8
  $past = @($history | Where-Object { $_.slug -eq $s })

  if (-not $st.ok) { Warn "$s reported problems: $(if ($errs.Count) { "$($errs[0])" } else { 'see ' + $log })" }
  $recent = @($past | Select-Object -Last ($StaleRuns - 1) | ForEach-Object { [int]$_.new }) + @([int]$st.new)
  if ($st.ok -and $recent.Count -ge $StaleRuns -and -not ($recent | Where-Object { $_ -ne 0 }) -and [int]$st.listed -ge $StaleMinListed) {
    Warn "$s found no new tenders in its last $StaleRuns runs (it lists $($st.listed)): the portal's list may have stopped updating"
  }
  $med = Median @($past | Where-Object { $_.ok -eq "True" -and [int]$_.listed -gt 0 } | Select-Object -Last 5 | ForEach-Object { [double]$_.listed })
  if ($st.ok -and $med -and [int]$st.listed -lt (1 - $DropShare) * $med) {
    Warn ("{0} lists {1} tenders, down from a median of {2} in its last runs" -f $s, $st.listed, [math]::Round($med))
  }
}

# ---- 4. commit
if (-not (Commit-Data)) { Done "nothing changed, nothing to publish" }
if ($NoPush) { Done "committed locally (-NoPush)" }

# ---- 5. push, re-applying our sources on top of a newer origin when needed
for ($try = 1; $try -le $MaxPushTries; $try++) {
  "=== $(Get-Date -Format s) git push origin master (try $try) ===" | Add-Content $gitLog -Encoding UTF8
  Git-Logged @("push","origin","master","-v","--progress")
  if ($script:GitCode -eq 0) { Done "pushed ($(git log -1 --format='%h %s'))" }
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
  Carry-OntoOrigin $Published
  Run-Index
  if (-not (Commit-Data)) { Done "origin already has everything" }
}
Abort "could not push after $MaxPushTries attempts (our commit is still local)"
