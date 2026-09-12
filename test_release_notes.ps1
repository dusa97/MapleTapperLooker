# Run with: powershell -NoProfile -File test_release_notes.ps1
$ErrorActionPreference = 'Stop'
$workflow = Get-Content "$PSScriptRoot/.github/workflows/release.yml" -Raw
$start = $workflow.IndexOf('          $previous =')
$end = $workflow.IndexOf('          gh release create', $start)
$script = [scriptblock]::Create($workflow.Substring($start, $end - $start))
$temporary = Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString())
$originalSha = $env:GITHUB_SHA
function gh { $script:LASTEXITCODE = 0; return $script:previousTag }
New-Item -ItemType Directory $temporary | Out-Null
Push-Location $temporary
try {
    git init --quiet
    git -c user.name=Test -c user.email=test@example.com commit --quiet --allow-empty -m 'Initial build'
    git tag build-1-1
    git -c user.name=Test -c user.email=test@example.com commit --quiet --allow-empty -m 'Add recorded alerts'
    $env:GITHUB_SHA = git rev-parse HEAD
    foreach ($case in @(
        @{ Tag = 'build-1-1'; Expected = '- Add recorded alerts'; Absent = '- Initial build' },
        @{ Tag = ''; Expected = '- Initial build'; Absent = 'no new changes' },
        @{ Tag = $env:GITHUB_SHA; Expected = '- Rebuild; no new changes.'; Absent = '- Add recorded alerts' }
    )) {
        $script:previousTag = $case.Tag
        & $script
        $notes = Get-Content release-notes.md -Raw
        if (-not $notes.Contains($case.Expected) -or $notes.Contains($case.Absent)) {
            throw "Incorrect release notes for previous tag '$($case.Tag)': $notes"
        }
    }
    Write-Output 'PASS: version changes, first release, and rebuild.'
} finally {
    $env:GITHUB_SHA = $originalSha
    Pop-Location
    Remove-Item -Recurse -Force $temporary
}
