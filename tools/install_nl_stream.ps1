[CmdletBinding(DefaultParameterSetName='Zip')]
param(
  [Parameter(Mandatory=$true)][string]$ProjectDirectory,
  [Parameter(Mandatory=$true,ParameterSetName='Zip')][string]$ModelModZip,
  [Parameter(Mandatory=$true,ParameterSetName='Unpacked')][string[]]$GameModDirectories
)
# Run from the update bundle, with game and companion server closed.
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem
$bundle=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$ProjectDirectory=[IO.Path]::GetFullPath($ProjectDirectory)
$zipMode=$PSCmdlet.ParameterSetName -eq 'Zip'
if($zipMode){$ModelModZip=[IO.Path]::GetFullPath($ModelModZip)}
$core='mod/beamng_earth/lua/ge/extensions/beamngEarth/core.lua'
$integrated=@('server/earthd.py',$core,'server/navgraph.py','mod/beamng_earth/lua/ge/extensions/beamngEarth/navgraph.lua','mod/beamng_earth/lua/ge/extensions/beamngEarth/roads.lua','mod/beamng_earth/lua/ge/extensions/beamngEarth/trees.lua')
$copied=@('server/nlsigns.py','mod/beamng_earth/lua/ge/extensions/beamngEarth/nlremote.lua','mod/beamng_earth/lua/ge/extensions/beamngEarth/nlndw.lua','mod/beamng_earth/lua/ge/extensions/beamngEarth/nl_context.lua','mod/beamng_earth/lua/ge/extensions/beamngEarth/nl_testset.lua','mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/nl_testset/size_profiles.json')
$copied+='mod/beamng_earth/lua/ge/extensions/beamngEarth/nl_placement.lua'
$copied+='mod/beamng_earth/lua/ge/extensions/beamngEarth/nl_legacy.lua'
$copied+='mod/beamng_earth/lua/ge/extensions/beamngEarth/nl_country.lua'
$copied+='mod/beamng_earth/lua/ge/extensions/beamngEarth/nl_replacements.lua'
$copied+='mod/beamng_earth/lua/ge/extensions/beamngEarth/nltestset.lua'
$files=$integrated+$copied
$gameSources=@($files | Where-Object {$_ -like 'mod/beamng_earth/*'})
foreach($relative in $integrated){if(-not(Test-Path -LiteralPath (Join-Path $ProjectDirectory $relative))){throw 'Select the existing BeamNG.Earth repository'}}
foreach($relative in $copied){if(-not(Test-Path -LiteralPath (Join-Path $bundle $relative))){throw 'Incomplete update bundle'}}
foreach($relative in $files){if(Test-Path -LiteralPath (Join-Path $ProjectDirectory $relative) -PathType Container){throw 'Expected a source file, found a directory'}}
if($zipMode -and -not(Test-Path -LiteralPath $ModelModZip -PathType Leaf)){throw 'Select the existing NL v9 model mod ZIP'}
$gameFiles=@()
if(-not $zipMode){
  foreach($directory in $GameModDirectories){
    $directory=[IO.Path]::GetFullPath($directory)
    if(-not(Test-Path -LiteralPath (Join-Path $directory 'lua/ge/extensions/beamngEarth/core.lua')) -and -not(Test-Path -LiteralPath (Join-Path $directory 'levels/beamng_earth/art/shapes/beamngEarth/nl_catalog/manifest.json'))){throw 'Select existing unpacked Earth/NL mod directories'}
    $directorySources=$gameSources
    # The integrated BGT core may be newer than the installed base mod. Ship its
    # complete existing base alongside it, including modules and their assets.
    # The NL model mod remains an overlay; its large model catalogue is retained.
    if((Split-Path $directory -Leaf) -eq 'beamng_earth'){
      $sourceRoot=Join-Path $ProjectDirectory 'mod/beamng_earth'
      $baseSources=@(Get-ChildItem -LiteralPath $sourceRoot -Recurse -File | Where-Object {$_.FullName -notmatch '[\\/]__pycache__[\\/]|\.pyc$'} | ForEach-Object {'mod/beamng_earth/'+$_.FullName.Substring($sourceRoot.Length+1).Replace('\','/')})
      $directorySources=@($gameSources+$baseSources | Select-Object -Unique)
    }
    foreach($relative in $directorySources){
      $gameFiles+=@{source=$relative;target=(Join-Path $directory $relative.Substring('mod/beamng_earth/'.Length))}
    }
  }
}
foreach($file in $gameFiles){if(Test-Path -LiteralPath $file.target -PathType Container){throw 'Expected a Lua file, found a directory'}}
$selected=$null;$alreadyApplied=$false
function Test-IntegrationPatch([string]$candidate,[switch]$Reverse){
  # Windows PowerShell treats git's expected diagnostic stderr as an error.
  # A rejected candidate is normal: continue to the compatible variant.
  $ErrorActionPreference='Continue'
  if($Reverse){$null=& git -c "safe.directory=$ProjectDirectory" -C $ProjectDirectory apply --reverse --check $candidate 2>&1}
  else{$null=& git -c "safe.directory=$ProjectDirectory" -C $ProjectDirectory apply --check $candidate 2>&1}
  return $LASTEXITCODE -eq 0
}
$candidates=@('integration.patch','integration-bgt.patch')
if(([IO.File]::ReadAllText((Join-Path $ProjectDirectory $core))).Contains('beamngEarth/nlremote') -and ([IO.File]::ReadAllText((Join-Path $ProjectDirectory 'server/earthd.py'))).Contains('import nlsigns')){
  $candidates+=@('upgrade-v11-base.patch','upgrade-v11-bgt.patch','upgrade-v10-base.patch','upgrade-v10-bgt.patch')
}
foreach($name in $candidates){
  $candidate=Join-Path $bundle $name
  if(-not(Test-Path -LiteralPath $candidate)){continue}
  if(Test-IntegrationPatch $candidate){$selected=$candidate;break}
  if(Test-IntegrationPatch $candidate -Reverse){$selected=$candidate;$alreadyApplied=$true;break}
}
if(-not $selected){throw 'This project version differs: no changes made. Merge the integration patch first.'}
$backup=Join-Path $ProjectDirectory ('backups/nl-streaming/'+[guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($backup) | Out-Null
$existing=@{}
foreach($relative in $files){
  $file=Join-Path $ProjectDirectory $relative
  $existing[$relative]=Test-Path -LiteralPath $file
  if($existing[$relative]){$save=Join-Path $backup $relative;[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($save)) | Out-Null;Copy-Item -LiteralPath $file -Destination $save}
}
$zipBackup=Join-Path $backup 'previous-model-mod.zip'
if($zipMode){Copy-Item -LiteralPath $ModelModZip -Destination $zipBackup}
$gameBackups=@{};$index=0
foreach($file in $gameFiles){
  $save=Join-Path $backup ('game-file-'+$index);$index++
  if(Test-Path -LiteralPath $file.target){Copy-Item -LiteralPath $file.target -Destination $save;$gameBackups[$file.target]=$save}
  else{$gameBackups[$file.target]=$null}
}
$archive=$null;$stream=$null;$failure=$null;$changed=$false
try{
  # Exclusive access fails before editing source if the game still holds the ZIP.
  if($zipMode){
    $stream=[IO.File]::Open($ModelModZip,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
    $archive=[IO.Compression.ZipArchive]::new($stream,[IO.Compression.ZipArchiveMode]::Update,$true)
    if(-not $archive.GetEntry('levels/beamng_earth/art/shapes/beamngEarth/nl_catalog/manifest.json')){throw 'This is not the NL v9 model mod'}
  }
  $changed=$true
  if(-not $alreadyApplied){
    & git -c "safe.directory=$ProjectDirectory" -C $ProjectDirectory apply $selected
    if($LASTEXITCODE -ne 0){throw 'Integration patch failed'}
  }
  foreach($relative in $copied){
    $target=Join-Path $ProjectDirectory $relative
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target)) | Out-Null
    Copy-Item -LiteralPath (Join-Path $bundle $relative) -Destination $target -Force
  }
  # Keep the local model/textures compressed. Update only code/size metadata,
  # including this repository's merged core, preserving its BGT changes.
  if($zipMode){foreach($relative in $gameSources){
    $name=$relative.Substring('mod/beamng_earth/'.Length)
    $old=$archive.GetEntry($name);if($old){$old.Delete()}
    $entry=$archive.CreateEntry($name,[IO.Compression.CompressionLevel]::Optimal)
    $entry.LastWriteTime=[DateTimeOffset]::UtcNow.AddHours(-15)
    $inputStream=[IO.File]::OpenRead((Join-Path $ProjectDirectory $relative));$outputStream=$entry.Open()
    try{$inputStream.CopyTo($outputStream)}finally{$outputStream.Dispose();$inputStream.Dispose()}
  }}else{
    foreach($file in $gameFiles){
      [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($file.target)) | Out-Null
      Copy-Item -LiteralPath (Join-Path $ProjectDirectory $file.source) -Destination $file.target -Force
    }
  }
}catch{$failure=$_}
finally{
  if($archive){try{$archive.Dispose()}catch{$failure=$_}}
  if($stream){$stream.Dispose()}
}
if($failure){
  if($changed){
    $restoreErrors=@()
    function Restore-PreviousFile($saved,$target){
      if($saved){
        if((Test-Path -LiteralPath $target -PathType Leaf) -and (Get-FileHash -LiteralPath $target).Hash -eq (Get-FileHash -LiteralPath $saved).Hash){return}
        Copy-Item -LiteralPath $saved -Destination $target -Force
      }elseif(Test-Path -LiteralPath $target -PathType Leaf){Remove-Item -LiteralPath $target}
    }
    if($zipMode){try{Copy-Item -LiteralPath $zipBackup -Destination $ModelModZip -Force}catch{$restoreErrors+=$_}}
    foreach($file in $gameFiles){
      try{Restore-PreviousFile $gameBackups[$file.target] $file.target}catch{$restoreErrors+=$_}
    }
    foreach($relative in $files){
      $target=Join-Path $ProjectDirectory $relative
      $saved=if($existing[$relative]){Join-Path $backup $relative}else{$null}
      try{Restore-PreviousFile $saved $target}catch{$restoreErrors+=$_}
    }
    if($restoreErrors.Count){Write-Warning "Some files could not be restored automatically. Original files are in $backup"}
  }
  throw $failure
}
Write-Output "Server and local model mod updated. Backup: $backup"
Write-Output 'Start the companion server with --nl-signs-store pointing to the four data ZIP files. No tile extraction is needed.'
