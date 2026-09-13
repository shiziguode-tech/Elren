param(
    [Parameter(Mandatory = $true)][string]$InputPath,
    [Parameter(Mandatory = $true)][string]$OutputPath,
    [string]$VoiceName = "",
    [double]$Rate = 1.0
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech
$text = [System.IO.File]::ReadAllText($InputPath, [System.Text.Encoding]::UTF8)
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    if (-not [string]::IsNullOrWhiteSpace($VoiceName)) {
        $installed = $speaker.GetInstalledVoices() | Where-Object {
            $_.Enabled -and $_.VoiceInfo.Name -eq $VoiceName
        } | Select-Object -First 1
        if ($null -ne $installed) {
            $speaker.SelectVoice($VoiceName)
        }
    }
    $mappedRate = [Math]::Round(($Rate - 1.0) * 6.0)
    $speaker.Rate = [Math]::Max(-5, [Math]::Min(6, [int]$mappedRate))
    $speaker.SetOutputToWaveFile($OutputPath)
    $speaker.Speak($text)
}
finally {
    $speaker.Dispose()
}
