param([string]$Manifest)
Add-Type -AssemblyName System.Speech
$voice = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voice.SelectVoice('Microsoft Huihui Desktop')
$voice.Rate = 1
$items = Get-Content -LiteralPath $Manifest -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($item in $items) {
    $voice.SetOutputToWaveFile($item.path)
    $voice.Speak($item.text)
    $voice.SetOutputToNull()
}
$voice.Dispose()
