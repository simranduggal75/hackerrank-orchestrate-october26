# OCR one image with the Windows built-in engine (Windows.Media.Ocr) at several scales.
# Output: JSON {"language", "width", "height", "passes": [{"scale", "words": [[text, x, y, w, h], ...]}]}
# Word boxes are reported in ORIGINAL image pixels (already divided by the scale).
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File ocr_windows.ps1 -Path img.png -Scales 2,3,4 -Out out.json
param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$Out,
    [string]$Scales = '2,3,4'
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
}
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) { throw 'no OCR engine for the user profile languages' }
$full = (Resolve-Path $Path).Path
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($full)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$width = [int]$decoder.PixelWidth
$height = [int]$decoder.PixelHeight
$passes = @()
foreach ($s in ($Scales -split ',')) {
    $scale = [int]$s
    $transform = New-Object Windows.Graphics.Imaging.BitmapTransform
    $transform.ScaledWidth = [uint32]($width * $scale)
    $transform.ScaledHeight = [uint32]($height * $scale)
    $transform.InterpolationMode = [Windows.Graphics.Imaging.BitmapInterpolationMode]::Fant
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync(
            [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8,
            [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied,
            $transform,
            [Windows.Graphics.Imaging.ExifOrientationMode]::IgnoreExifOrientation,
            [Windows.Graphics.Imaging.ColorManagementMode]::DoNotColorManage)) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    $words = New-Object System.Collections.ArrayList
    foreach ($line in $result.Lines) {
        foreach ($w in $line.Words) {
            $r = $w.BoundingRect
            [void]$words.Add(@($w.Text, [math]::Round($r.X / $scale, 1), [math]::Round($r.Y / $scale, 1),
                    [math]::Round($r.Width / $scale, 1), [math]::Round($r.Height / $scale, 1)))
        }
    }
    $passes += [ordered]@{ scale = $scale; words = $words }
}
$doc = [ordered]@{ language = $engine.RecognizerLanguage.LanguageTag; width = $width; height = $height; passes = $passes }
$json = $doc | ConvertTo-Json -Depth 6 -Compress
[System.IO.File]::WriteAllText($Out, $json, (New-Object System.Text.UTF8Encoding($false)))
