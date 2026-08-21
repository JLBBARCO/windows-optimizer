Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-RequestedBranch {
	$pattern = 'raw\.githubusercontent\.com/JLBBARCO/windows-optimizer/(?<branch>main|beta)/core-app/run\.ps1'

	try {
		$historyLines = @(Get-History -Count 30 | Select-Object -ExpandProperty CommandLine)
		for ($i = $historyLines.Count - 1; $i -ge 0; $i--) {
			$line = [string]$historyLines[$i]
			if ($line -match $pattern) {
				return $Matches.branch
			}
		}
	}
	catch {
		# If history is unavailable (restricted hosts), fallback below.
	}

	return 'main'
}

function Get-LatestReleaseForBranch {
	param(
		[Parameter(Mandatory = $true)]
		[ValidateSet('main', 'beta')]
		[string]$Branch
	)

	$repo = 'JLBBARCO/windows-optimizer'
	$uri = "https://api.github.com/repos/$repo/releases?per_page=100"
	$headers = @{
		'Accept'     = 'application/vnd.github+json'
		'User-Agent' = 'windows-optimizer-runner'
	}

	$allReleases = @(Invoke-RestMethod -Uri $uri -Headers $headers -Method Get)
	$filtered = $allReleases | Where-Object {
		if ($Branch -eq 'beta') {
			$_.draft -eq $false -and $_.prerelease -eq $true
		}
		else {
			$_.draft -eq $false -and $_.prerelease -eq $false
		}
	} | Sort-Object -Property published_at -Descending

	if (-not $filtered -or $filtered.Count -eq 0) {
		throw "No matching release found for branch '$Branch'."
	}

	return $filtered[0]
}

function Get-ExecutableAsset {
	param(
		[Parameter(Mandatory = $true)]
		$Release
	)

	$exeAsset = $Release.assets | Where-Object { $_.name -like '*.exe' } | Select-Object -First 1
	if (-not $exeAsset) {
		throw "Release '$($Release.tag_name)' does not contain an executable asset (*.exe)."
	}

	return $exeAsset
}

function Invoke-EphemeralExecutable {
	param(
		[Parameter(Mandatory = $true)]
		[string]$DownloadUrl,

		[Parameter(Mandatory = $true)]
		[string]$AssetName
	)

	$tempExe = Join-Path -Path $env:TEMP -ChildPath ("windows-optimizer-" + [guid]::NewGuid().ToString('N') + ".exe")

	try {
		Write-Host "Downloading $AssetName to temporary path..."
		Invoke-WebRequest -Uri $DownloadUrl -OutFile $tempExe

		Write-Host 'Running executable from temporary location...'
		Start-Process -FilePath $tempExe -Wait
	}
	finally {
		if (Test-Path -LiteralPath $tempExe) {
			Remove-Item -LiteralPath $tempExe -Force -ErrorAction SilentlyContinue
		}
	}
}

$requestedBranch = Get-RequestedBranch
Write-Host "Detected branch context: $requestedBranch"

$release = Get-LatestReleaseForBranch -Branch $requestedBranch
$asset = Get-ExecutableAsset -Release $release

Write-Host "Selected tag: $($release.tag_name)"
Invoke-EphemeralExecutable -DownloadUrl $asset.browser_download_url -AssetName $asset.name
