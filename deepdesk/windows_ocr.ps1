param(
    [string]$ImagePath = "",
    [switch]$StatusOnly,
    [string[]]$LanguageTags = @()
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime

[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null

if ($StatusOnly) {
    $statusLanguages = @(
        [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages |
            ForEach-Object { $_.LanguageTag }
    )
    [ordered]@{
        ok = ($statusLanguages.Count -gt 0)
        source = "Windows.Media.Ocr"
        offline = $true
        available_languages = $statusLanguages
        error = if ($statusLanguages.Count -gt 0) { "" } else { "No Windows OCR language pack is installed" }
    } | ConvertTo-Json -Depth 4 -Compress
    exit 0
}

$asTask = (
    [System.WindowsRuntimeSystemExtensions].GetMethods() |
        Where-Object {
            $_.Name -eq "AsTask" -and
            $_.IsGenericMethod -and
            $_.GetParameters().Count -eq 1
        }
)[0]

function Await-WinRT($Operation, [Type]$ResultType) {
    $method = $asTask.MakeGenericMethod($ResultType)
    $task = $method.Invoke($null, @($Operation))
    $task.Wait()
    return $task.Result
}

$resolvedPath = [IO.Path]::GetFullPath($ImagePath)
if (-not [IO.File]::Exists($resolvedPath)) {
    throw "Image does not exist: $resolvedPath"
}

$file = Await-WinRT (
    [Windows.Storage.StorageFile]::GetFileFromPathAsync($resolvedPath)
) ([Windows.Storage.StorageFile])
$stream = Await-WinRT (
    $file.OpenAsync([Windows.Storage.FileAccessMode]::Read)
) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await-WinRT (
    [Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)
) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await-WinRT (
    $decoder.GetSoftwareBitmapAsync()
) ([Windows.Graphics.Imaging.SoftwareBitmap])

if (
    $bitmap.PixelWidth -gt [Windows.Media.Ocr.OcrEngine]::MaxImageDimension -or
    $bitmap.PixelHeight -gt [Windows.Media.Ocr.OcrEngine]::MaxImageDimension
) {
    throw (
        "Image exceeds Windows OCR limit of {0}px: {1}x{2}" -f
        [Windows.Media.Ocr.OcrEngine]::MaxImageDimension,
        $bitmap.PixelWidth,
        $bitmap.PixelHeight
    )
}

$available = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages)
$availableTags = @($available | ForEach-Object { $_.LanguageTag })

function Resolve-OcrLanguageTag([string]$RequestedTag) {
    if ([string]::IsNullOrWhiteSpace($RequestedTag)) { return $null }
    $exact = $availableTags | Where-Object { $_ -ieq $RequestedTag } | Select-Object -First 1
    if ($null -ne $exact) { return $exact }
    $prefix = $availableTags | Where-Object {
        $_ -ilike ("{0}-*" -f $RequestedTag)
    } | Select-Object -First 1
    if ($null -ne $prefix) { return $prefix }
    $base = ($RequestedTag -split "-")[0]
    return $availableTags | Where-Object {
        $_ -ieq $base -or $_ -ilike ("{0}-*" -f $base)
    } | Select-Object -First 1
}

$selectedTags = @(
    if ($LanguageTags.Count -gt 0) {
        $LanguageTags |
            ForEach-Object { Resolve-OcrLanguageTag $_ } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
            Select-Object -Unique
    } else {
        # Never run every installed language pack for a single frame. The user
        # interface language is primary and English is the only default fallback.
        @(
            [Globalization.CultureInfo]::CurrentUICulture.Name,
            [Globalization.CultureInfo]::CurrentUICulture.TwoLetterISOLanguageName,
            "en-US"
        ) |
            ForEach-Object { Resolve-OcrLanguageTag $_ } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
            Select-Object -Unique -First 2
    }
)
if ($selectedTags.Count -eq 0) {
    throw "No requested Windows OCR language pack is installed"
}

$candidates = @()
foreach ($tag in $selectedTags) {
    $language = [Windows.Globalization.Language]::new($tag)
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
    if ($null -eq $engine) { continue }
    $ocrResult = Await-WinRT ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    $lines = @()
    foreach ($line in $ocrResult.Lines) {
        $words = @()
        foreach ($word in $line.Words) {
            $rect = $word.BoundingRect
            $words += [ordered]@{
                text = $word.Text
                box = @(
                    [Math]::Round($rect.X, 2),
                    [Math]::Round($rect.Y, 2),
                    [Math]::Round($rect.Width, 2),
                    [Math]::Round($rect.Height, 2)
                )
            }
        }
        $lines += [ordered]@{
            text = $line.Text
            words = $words
        }
    }
    $candidates += [ordered]@{
        language = $tag
        text = $ocrResult.Text
        lines = $lines
        text_length = $ocrResult.Text.Length
    }
}

$primary = $candidates | Sort-Object text_length -Descending | Select-Object -First 1
$payload = [ordered]@{
    ok = $true
    source = "Windows.Media.Ocr"
    offline = $true
    image = [ordered]@{
        width = $bitmap.PixelWidth
        height = $bitmap.PixelHeight
    }
    available_languages = $availableTags
    selected_languages = $selectedTags
    primary_language = if ($null -ne $primary) { $primary.language } else { "" }
    text = if ($null -ne $primary) { $primary.text } else { "" }
    candidates = $candidates
}

$payload | ConvertTo-Json -Depth 12 -Compress
