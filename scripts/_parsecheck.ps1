param(
  [Parameter(Mandatory = $true)][string]$Path,
  [switch]$DumpStrings
)
$tokens = $null
$errors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $Path).Path, [ref]$tokens, [ref]$errors)
if ($errors -and $errors.Count -gt 0) {
  foreach ($e in $errors) { Write-Output ("ERROR line {0}: {1}" -f $e.Extent.StartLineNumber, $e.Message) }
  exit 1
}
Write-Output "PARSE OK"
if ($DumpStrings) {
  Write-Output '--- string literals ---'
  $tokens | Where-Object { $_ -is [System.Management.Automation.Language.StringToken] } |
    Select-Object -First 8 | ForEach-Object { Write-Output $_.Value }
}
exit 0
