param(
  [Parameter(Mandatory=$true)][string]$Store,
  [Parameter(Mandatory=$true)][string]$OutputDirectory,
  # Only for resuming an interrupted run of exactly the same source/data build.
  [switch]$ReuseCompleted
)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem
$repo=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$Store=[IO.Path]::GetFullPath($Store)
$OutputDirectory=[IO.Path]::GetFullPath($OutputDirectory)
$manifest=Get-Content -Raw -LiteralPath (Join-Path $Store 'manifest.json') | ConvertFrom-Json
if($manifest.kind -ne 'george_v5_tile_store' -or $manifest.source.scope -ne 'Netherlands, all owners'){throw 'Require a completed national store'}
[IO.Directory]::CreateDirectory($OutputDirectory) | Out-Null
function Write-Archive([string]$Target,$Rows){
  if(Test-Path -LiteralPath $Target){
    if(-not $ReuseCompleted){throw "Refusing to replace $Target; use a new output directory"}
    $existing=[IO.Compression.ZipFile]::OpenRead($Target)
    try{
      if($existing.Entries.Count -ne $Rows.Count){throw 'Existing archive does not match this build'}
      foreach($row in $Rows){if(-not $existing.GetEntry($row.name)){throw 'Existing archive does not match this build'}}
    }finally{$existing.Dispose()}
    Write-Host ('Reusing complete archive '+[IO.Path]::GetFileName($Target));return
  }
  # Buffer output so thousands of tiny inputs do not alternate disk reads/writes.
  $outputStream=[IO.File]::Open($Target+'.part',[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
  $buffer=[IO.BufferedStream]::new($outputStream,16777216)
  $zip=[IO.Compression.ZipArchive]::new($buffer,[IO.Compression.ZipArchiveMode]::Create,$false)
  $count=0;$bytes=[long]0
  try{
    foreach($row in $Rows){
      if($row.name.Contains('..') -or $row.name.StartsWith('/') -or $row.name.Contains('\')){throw 'Unsafe archive entry'}
      $entry=$zip.CreateEntry($row.name,[IO.Compression.CompressionLevel]::Optimal)
      $entry.LastWriteTime=[DateTimeOffset]::UtcNow.AddHours(-15)
      $inputStream=[IO.File]::OpenRead($row.file)
      $bytes+=$inputStream.Length
      $entryStream=$entry.Open()
      try{$inputStream.CopyTo($entryStream)}finally{$entryStream.Dispose();$inputStream.Dispose()}
      $count++
    }
  }finally{$zip.Dispose()}
  $check=[IO.Compression.ZipFile]::OpenRead($Target+'.part')
  try{
    if($check.Entries.Count -ne $count){throw 'Archive entry count mismatch'}
    $names=[Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    $actual=[long]0
    foreach($entry in $check.Entries){if(-not $names.Add($entry.FullName)){throw 'Duplicate archive entry'};$actual+=$entry.Length}
    if($actual -ne $bytes){throw 'Archive size mismatch'}
  }finally{$check.Dispose()}
  Move-Item -LiteralPath ($Target+'.part') -Destination $Target
  Write-Host ([IO.Path]::GetFileName($Target)+": $count entries, $bytes uncompressed bytes")
}
$changed=@(& git -c "safe.directory=$repo" -C $repo diff --name-only HEAD)
if($LASTEXITCODE -ne 0){throw 'Cannot list changed source files'}
$new=@(& git -c "safe.directory=$repo" -C $repo ls-files --others --exclude-standard)
if($LASTEXITCODE -ne 0){throw 'Cannot list new source files'}
$files=@(($changed+$new) | Sort-Object -Unique | Where-Object {Test-Path -LiteralPath (Join-Path $repo $_) -PathType Leaf})
$modRows=@($files | Where-Object {$_ -like 'mod/beamng_earth/*'} | ForEach-Object {@{file=(Join-Path $repo $_);name=$_.Substring('mod/beamng_earth/'.Length)}})
if($modRows.Count -lt 2500){throw 'Catalogue missing from mod overlay'}
Write-Archive (Join-Path $OutputDirectory 'BeamNG_Earth_NL_v9_mod.zip') $modRows
$sourceRows=@($files | ForEach-Object {@{file=(Join-Path $repo $_);name=$_}})
Write-Archive (Join-Path $OutputDirectory 'BeamNG_Earth_NL_v9_bron.zip') $sourceRows
$tiles=@([IO.Directory]::EnumerateFiles((Join-Path $Store 'render'),'g17_*.json') | Sort-Object)
if($tiles.Count -ne $manifest.totals.render_tiles){throw 'National tile count mismatch'}
$dataRows=@(@{file=(Join-Path $Store 'manifest.json');name='levels/beamng_earth/data/george_nederland/manifest.json'})
$dataRows+=@($tiles | ForEach-Object {@{file=$_;name=('levels/beamng_earth/data/george_nederland/render/'+[IO.Path]::GetFileName($_))}})
# Each game archive stays below the classic ZIP 65535-entry limit.
$packs=@()
for($offset=0;$offset -lt $dataRows.Count;$offset+=50000){
  $last=[Math]::Min($offset+49999,$dataRows.Count-1)
  $name='BeamNG_Earth_Nederland_v9_data_{0:00}.zip' -f ($packs.Count+1)
  $file=Join-Path $OutputDirectory $name
  Write-Archive $file $dataRows[$offset..$last]
  $packs+=@{file=$file;name=$name}
}
$instructions=Join-Path $OutputDirectory 'Nederland_v9_installatie_en_resultaten.md'
if(-not(Test-Path -LiteralPath $instructions)){throw 'Missing installation instructions'}
$packs+=@{file=$instructions;name='Nederland_v9_installatie_en_resultaten.md'}
Write-Archive (Join-Path $OutputDirectory 'BeamNG_Earth_Nederland_v9_datapakket.zip') $packs
$results=@('BeamNG_Earth_NL_v9_mod.zip','BeamNG_Earth_NL_v9_bron.zip','BeamNG_Earth_Nederland_v9_datapakket.zip') | ForEach-Object {Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $OutputDirectory $_)}
$results | ForEach-Object {$_.Hash.ToLower()+'  '+[IO.Path]::GetFileName($_.Path)} | Set-Content -Encoding utf8 -LiteralPath (Join-Path $OutputDirectory 'V9_SHA256.txt')
