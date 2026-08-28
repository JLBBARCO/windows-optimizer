param(
	[ValidateSet('main', 'beta')]
	[string]$Branch,

	# Extra arguments forwarded to the application (e.g. --simulate).
	[Parameter(ValueFromRemainingArguments = $true)]
	[string[]]$AppArguments
)

<#
.SYNOPSIS
	One-line launcher for Windows Optimizer.

.DESCRIPTION
	Designed to be executed as:

		irm https://raw.githubusercontent.com/JLBBARCO/windows-optimizer/main/core-app/run.ps1 | iex
		irm https://raw.githubusercontent.com/JLBBARCO/windows-optimizer/beta/core-app/run.ps1 | iex

	Channel resolution
	------------------
	The branch present in the URL that fetched THIS script decides which
	GitHub release is executed:

		main -> newest published release      (prerelease = false)
		beta -> newest published pre-release  (prerelease = true)

	Because the script is piped into `iex` it has no path of its own, so the
	branch is resolved from several independent sources, in order of
	reliability: -Branch, %WO_BRANCH%, the invocation line, the command line of
	the current PowerShell process (this is what makes the Desktop shortcut
	work), the session history, and finally a content fingerprint of this very
	script compared against the raw run.ps1 of both branches. When nothing can
	be proven, 'main' is used.

	Execution model
	---------------
	The application is a Python program and is NOT packaged into an executable:
	the launcher always runs it directly from the source code of the release
	selected for the channel.

		1. resolve the release for the channel through the GitHub API;
		2. download the source archive of that exact tag into MEMORY;
		3. expand only `core-app/` into %LOCALAPPDATA%\windows-optimizer\src\<tag>
		   (never %TEMP%, which the maintenance plan cleans);
		4. run `python core-app\main.py` in the current console and return its
		   exit code;
		5. remove the staging folder when the run ends.

	Requirement: Python 3.10 or later must be installed. There is no executable
	fallback; the launcher stops with an explicit message when no usable
	interpreter is found.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$script:Repository = 'JLBBARCO/windows-optimizer'
$script:BranchPattern = 'raw\.githubusercontent\.com/JLBBARCO/windows-optimizer/(?<branch>main|beta)/core-app/run\.ps1'
$script:MinimumPython = [Version]'3.10'
$script:ApiHeaders = @{
	'Accept'     = 'application/vnd.github+json'
	'User-Agent' = 'windows-optimizer-runner'
}

# $MyInvocation is scope sensitive: inside a function it describes the function
# call, not the launcher. Capture the script-level values once, here.
$script:InvocationLine = ''
$script:OwnScriptText = $null
try { $script:InvocationLine = [string]$MyInvocation.Line } catch { }
try {
	if ($MyInvocation.MyCommand -and $MyInvocation.MyCommand.ScriptBlock) {
		$script:OwnScriptText = $MyInvocation.MyCommand.ScriptBlock.ToString()
	}
}
catch { }

function Initialize-Tls {
	try {
		$current = [Net.ServicePointManager]::SecurityProtocol
		if (($current -band [Net.SecurityProtocolType]::Tls12) -ne [Net.SecurityProtocolType]::Tls12) {
			[Net.ServicePointManager]::SecurityProtocol = $current -bor [Net.SecurityProtocolType]::Tls12
		}
	}
	catch {
		# Modern PowerShell versions negotiate TLS on their own.
	}
}

# ------------------------------------------------------------- branch detection
function Get-BranchFromText {
	param([string]$Text)

	if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
	if ($Text -match $script:BranchPattern) { return $Matches.branch }
	return $null
}

function Get-BranchFromEnvironment {
	$value = $env:WO_BRANCH
	if ([string]::IsNullOrWhiteSpace($value)) { $value = $env:WO_CHANNEL }
	if ([string]::IsNullOrWhiteSpace($value)) { return $null }

	switch ($value.Trim().ToLowerInvariant()) {
		'main' { return 'main' }
		'release' { return 'main' }
		'stable' { return 'main' }
		'beta' { return 'beta' }
		'prerelease' { return 'beta' }
		'pre-release' { return 'beta' }
		default { return $null }
	}
}

function Get-BranchFromInvocation {
	$branch = Get-BranchFromText -Text $script:InvocationLine
	if ($branch) { return $branch }

	try {
		if ($PSCommandPath) {
			$branch = Get-BranchFromText -Text ([string]$PSCommandPath)
			if ($branch) { return $branch }
		}
	}
	catch { }

	return $null
}

function Get-BranchFromProcessCommandLine {
	# Covers the Desktop shortcut: powershell.exe -Command "irm <url> | iex".
	try {
		$process = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $PID" -ErrorAction Stop
		return Get-BranchFromText -Text ([string]$process.CommandLine)
	}
	catch {
		return $null
	}
}

function Get-BranchFromHistory {
	try {
		$lines = @(Get-History -Count 50 -ErrorAction Stop | Select-Object -ExpandProperty CommandLine)
		for ($i = $lines.Count - 1; $i -ge 0; $i--) {
			$branch = Get-BranchFromText -Text ([string]$lines[$i])
			if ($branch) { return $branch }
		}
	}
	catch {
		# History is unavailable in restricted hosts.
	}
	return $null
}

function Get-OwnSourceText {
	# Inside `iex` the script has no file, but the executing script block still
	# exposes its own text.
	try {
		if ($PSCommandPath -and (Test-Path -LiteralPath $PSCommandPath)) {
			return Get-Content -LiteralPath $PSCommandPath -Raw
		}
	}
	catch { }

	return $script:OwnScriptText
}

function Get-TextFingerprint {
	param([string]$Text)

	if ([string]::IsNullOrWhiteSpace($Text)) { return $null }

	# Normalize line endings and trailing whitespace: raw.githubusercontent and
	# the local checkout may differ only by CRLF/LF.
	$normalized = $Text -replace "`r`n", "`n"
	$normalized = $normalized.TrimEnd()

	$sha = [System.Security.Cryptography.SHA256]::Create()
	try {
		$bytes = [Text.Encoding]::UTF8.GetBytes($normalized)
		return -join ($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') })
	}
	finally {
		$sha.Dispose()
	}
}

function Get-BranchFromFingerprint {
	$ownText = Get-OwnSourceText
	$ownHash = Get-TextFingerprint -Text $ownText
	if (-not $ownHash) { return $null }

	$matched = @()
	foreach ($candidate in 'main', 'beta') {
		try {
			$uri = "https://raw.githubusercontent.com/$($script:Repository)/$candidate/core-app/run.ps1"
			$remote = Invoke-RestMethod -Uri $uri -Headers @{ 'User-Agent' = 'windows-optimizer-runner' } -Method Get
			$remoteHash = Get-TextFingerprint -Text ([string]$remote)
			if ($remoteHash -and $remoteHash -eq $ownHash) { $matched += $candidate }
		}
		catch {
			# A missing branch simply cannot match.
		}
	}

	# Only a single unambiguous match proves the channel: when both branches
	# hold identical content, the fingerprint carries no information.
	if ($matched.Count -eq 1) { return $matched[0] }
	return $null
}

function Resolve-Branch {
	param([string]$Requested)

	if ($Requested) {
		Write-Host "Channel from -Branch: $Requested"
		return $Requested
	}

	$resolvers = [ordered]@{
		'environment variable' = { Get-BranchFromEnvironment }
		'invocation line'      = { Get-BranchFromInvocation }
		'process command line' = { Get-BranchFromProcessCommandLine }
		'session history'      = { Get-BranchFromHistory }
		'content fingerprint'  = { Get-BranchFromFingerprint }
	}

	foreach ($name in $resolvers.Keys) {
		$branch = & $resolvers[$name]
		if ($branch) {
			Write-Host "Channel detected from ${name}: $branch"
			return $branch
		}
	}

	Write-Host 'Channel could not be detected; defaulting to main.'
	return 'main'
}

# -------------------------------------------------------------------- releases
function Get-PropertyValue {
	# StrictMode turns a missing property into a terminating error, and the GitHub
	# API answers with a completely different shape when it rate limits a caller.
	param($InputObject, [string]$Name, $Default = $null)

	if ($null -eq $InputObject) { return $Default }
	try {
		$property = $InputObject.PSObject.Properties[$Name]
		if ($null -eq $property) { return $Default }
		if ($null -eq $property.Value) { return $Default }
		return $property.Value
	}
	catch {
		return $Default
	}
}

function Get-LatestReleaseForBranch {
	param(
		[Parameter(Mandatory = $true)]
		[ValidateSet('main', 'beta')]
		[string]$Branch
	)

	$uri = "https://api.github.com/repos/$($script:Repository)/releases?per_page=100"
	$response = Invoke-RestMethod -Uri $uri -Headers $script:ApiHeaders -Method Get

	$apiMessage = Get-PropertyValue -InputObject $response -Name 'message'
	if ($apiMessage) {
		throw "GitHub API error while listing releases: $apiMessage"
	}

	$wantPrerelease = ($Branch -eq 'beta')
	$candidates = New-Object System.Collections.Generic.List[object]

	foreach ($release in @($response)) {
		if ([bool](Get-PropertyValue -InputObject $release -Name 'draft' -Default $false)) { continue }
		if (-not (Get-PropertyValue -InputObject $release -Name 'tag_name')) { continue }
		if ([bool](Get-PropertyValue -InputObject $release -Name 'prerelease' -Default $false) -ne $wantPrerelease) { continue }
		$candidates.Add($release)
	}

	$expected = if ($wantPrerelease) { 'pre-release' } else { 'release' }
	if ($candidates.Count -eq 0) {
		throw "No published $expected found in $($script:Repository) for branch '$Branch'."
	}

	$sorted = @(
		$candidates | Sort-Object -Property @{
			Expression = {
				$value = Get-PropertyValue -InputObject $_ -Name 'published_at'
				if ($value) { [datetime]$value } else { [datetime]::MinValue }
			}
		} -Descending
	)

	return $sorted[0]
}

# ---------------------------------------------------------------- python setup
function Get-PythonCommand {
	$candidates = New-Object System.Collections.Generic.List[object]

	$launcher = Get-Command -Name 'py.exe' -CommandType Application -ErrorAction SilentlyContinue |
		Select-Object -First 1
	if ($launcher) {
		$candidates.Add([pscustomobject]@{ Executable = $launcher.Source; Prefix = @('-3') })
	}

	foreach ($name in 'python.exe', 'python3.exe') {
		$found = Get-Command -Name $name -CommandType Application -ErrorAction SilentlyContinue
		foreach ($item in @($found)) {
			if ($item) {
				$candidates.Add([pscustomobject]@{ Executable = $item.Source; Prefix = @() })
			}
		}
	}

	foreach ($candidate in $candidates) {
		try {
			$arguments = @($candidate.Prefix) + @('-c', 'import sys;print("%d.%d.%d" % sys.version_info[:3])')
			$output = & $candidate.Executable @arguments 2>$null
			if ($LASTEXITCODE -ne 0 -or -not $output) { continue }

			$text = ([string](@($output)[0])).Trim()
			$version = [Version]$text
			if ($version -lt $script:MinimumPython) {
				Write-Host "Ignoring Python $version at $($candidate.Executable) (3.10+ required)."
				continue
			}

			return [pscustomobject]@{
				Executable = $candidate.Executable
				Prefix     = $candidate.Prefix
				Version    = $version
			}
		}
		catch {
			# Microsoft Store stubs and broken installs land here.
			continue
		}
	}

	return $null
}

# -------------------------------------------------------------- source staging
function Get-SourceArchiveBytes {
	param(
		[Parameter(Mandatory = $true)]
		$Release
	)

	$urls = New-Object System.Collections.Generic.List[string]
	$zipball = Get-PropertyValue -InputObject $Release -Name 'zipball_url'
	if ($zipball) { $urls.Add([string]$zipball) }
	$urls.Add("https://github.com/$($script:Repository)/archive/refs/tags/$($Release.tag_name).zip")

	foreach ($url in $urls) {
		try {
			Write-Host "Fetching source of $($Release.tag_name) into memory..."
			$response = Invoke-WebRequest -Uri $url -Headers $script:ApiHeaders -Method Get -UseBasicParsing

			# Depending on the PowerShell version the payload is exposed either as a
			# byte array or only through the raw stream.
			try {
				if ($response.RawContentStream) {
					$bytes = $response.RawContentStream.ToArray()
					if ($bytes.Length -gt 0) { return [byte[]]$bytes }
				}
			}
			catch { }

			if ($response.Content -is [byte[]] -and $response.Content.Length -gt 0) {
				return [byte[]]$response.Content
			}
		}
		catch {
			Write-Host "Source download failed for $url ($($_.Exception.Message))."
		}
	}

	throw "Could not download the source archive of $($Release.tag_name)."
}

function Expand-CoreAppFromMemory {
	param(
		[Parameter(Mandatory = $true)]
		[byte[]]$Bytes,

		[Parameter(Mandatory = $true)]
		[string]$Destination
	)

	Add-Type -AssemblyName 'System.IO.Compression' -ErrorAction SilentlyContinue
	Add-Type -AssemblyName 'System.IO.Compression.FileSystem' -ErrorAction SilentlyContinue

	$stream = New-Object System.IO.MemoryStream -ArgumentList @(, $Bytes)
	$archive = $null
	$extracted = 0

	try {
		$archive = New-Object System.IO.Compression.ZipArchive(
			$stream, [System.IO.Compression.ZipArchiveMode]::Read)

		foreach ($entry in $archive.Entries) {
			if ([string]::IsNullOrEmpty($entry.Name)) { continue }

			# Entries look like "JLBBARCO-windows-optimizer-<sha>/core-app/main.py".
			$relative = $entry.FullName
			$separator = $relative.IndexOf('/')
			if ($separator -lt 0) { continue }
			$relative = $relative.Substring($separator + 1)

			if (-not $relative.StartsWith('core-app/', [StringComparison]::OrdinalIgnoreCase)) { continue }

			$target = Join-Path -Path $Destination -ChildPath ($relative -replace '/', '\')
			$parent = Split-Path -Path $target -Parent
			if (-not (Test-Path -LiteralPath $parent)) {
				New-Item -ItemType Directory -Path $parent -Force | Out-Null
			}

			[System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
			$extracted++
		}
	}
	finally {
		if ($archive) { $archive.Dispose() }
		$stream.Dispose()
	}

	if ($extracted -eq 0) {
		throw 'The source archive did not contain a core-app folder.'
	}

	Write-Host "Prepared $extracted source file(s)."
}

function Invoke-ApplicationFromSource {
	param(
		[Parameter(Mandatory = $true)]
		$Release,

		[Parameter(Mandatory = $true)]
		$Python,

		[Parameter(Mandatory = $true)]
		[string]$Branch,

		[string[]]$Arguments
	)

	# %TEMP% is wiped by the maintenance plan, so the sources live under
	# %LOCALAPPDATA%, which the application also exports as a protected path.
	$root = Join-Path -Path $env:LOCALAPPDATA -ChildPath 'windows-optimizer\src'
	$stage = Join-Path -Path $root -ChildPath ("$($Release.tag_name)-" + [guid]::NewGuid().ToString('N'))

	$bytes = Get-SourceArchiveBytes -Release $Release
	New-Item -ItemType Directory -Path $stage -Force | Out-Null

	try {
		Expand-CoreAppFromMemory -Bytes $bytes -Destination $stage

		$entryPoint = Join-Path -Path $stage -ChildPath 'core-app\main.py'
		if (-not (Test-Path -LiteralPath $entryPoint)) {
			throw "Entry point not found: $entryPoint"
		}

		$env:WO_BRANCH = $Branch
		$env:WO_CHANNEL = if ($Branch -eq 'beta') { 'pre-release' } else { 'release' }
		$env:WO_RELEASE_TAG = [string]$Release.tag_name

		# The staging folder is removed when the run ends, so the logs must be
		# written outside of it.
		if (-not $env:WO_LOG_DIR) {
			$env:WO_LOG_DIR = Join-Path -Path $env:LOCALAPPDATA -ChildPath 'windows-optimizer\logs'
		}

		Write-Host "Running Windows Optimizer $($Release.tag_name) from source with Python $($Python.Version)..."

		$invocation = @($Python.Prefix) + @($entryPoint) + @($Arguments | Where-Object { $_ })
		& $Python.Executable @invocation
		return $LASTEXITCODE
	}
	finally {
		try {
			Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
		}
		catch { }
	}
}

# -------------------------------------------------------------------- main flow
Initialize-Tls

$resolvedBranch = Resolve-Branch -Requested $Branch
$release = Get-LatestReleaseForBranch -Branch $resolvedBranch

$channelLabel = if ($resolvedBranch -eq 'beta') { 'latest pre-release' } else { 'latest release' }
Write-Host "Selected $channelLabel for '$resolvedBranch': $($release.tag_name)"

$python = Get-PythonCommand
if (-not $python) {
	Write-Warning "Python $($script:MinimumPython) or later was not found on this machine."
	Write-Host 'Windows Optimizer runs directly from its Python source code, so an interpreter is required.'
	Write-Host 'Install it with:  winget install --id Python.Python.3.12 --source winget'
	Write-Host 'or download it from https://www.python.org/downloads/windows/ and run this launcher again.'
	$global:LASTEXITCODE = 1
	return
}

$exitCode = Invoke-ApplicationFromSource -Release $release -Python $python -Branch $resolvedBranch -Arguments $AppArguments

if ($null -eq $exitCode) { $exitCode = 0 }
Write-Host "Windows Optimizer finished with exit code $exitCode."

# No `exit` here on purpose: the script is normally piped into `iex` inside an
# interactive session and `exit` would close the user's console. The code is
# published through $global:LASTEXITCODE instead.
$global:LASTEXITCODE = $exitCode
