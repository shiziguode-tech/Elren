param(
    [string]$SigningCertificateThumbprint = $env:ELREN_SIGNING_CERT_THUMBPRINT,
    [string]$TimestampServer = "http://timestamp.digicert.com",
    [switch]$RequireSigned
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Source = Join-Path $ProjectRoot "launcher\ElrenLauncher.cs"
$Icon = Join-Path $ProjectRoot "launcher\elren.ico"
$SplashIcon = Join-Path $ProjectRoot "launcher\elren-app-icon.png"
$Output = Join-Path $ProjectRoot "Elren.exe"
$Compiler = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
$WebViewRuntime = Join-Path $ProjectRoot "launcher\webview2"
$WebViewCore = Join-Path $WebViewRuntime "Microsoft.Web.WebView2.Core.dll"
$WebViewForms = Join-Path $WebViewRuntime "Microsoft.Web.WebView2.WinForms.dll"
$WebViewLoader = Join-Path $WebViewRuntime "WebView2Loader.dll"

if (-not (Test-Path -LiteralPath $Compiler)) {
    throw "The .NET Framework C# compiler was not found: $Compiler"
}
if (-not (Test-Path -LiteralPath $Icon)) {
    throw "The launcher icon was not found: $Icon"
}
if (-not (Test-Path -LiteralPath $SplashIcon)) {
    throw "The launcher splash icon was not found: $SplashIcon"
}
foreach ($dependency in @($WebViewCore, $WebViewForms, $WebViewLoader)) {
    if (-not (Test-Path -LiteralPath $dependency)) {
        throw "The WebView2 desktop dependency was not found: $dependency"
    }
}

& $Compiler /nologo /target:winexe /optimize+ /platform:x64 `
    /reference:System.dll /reference:System.Drawing.dll /reference:System.Windows.Forms.dll /reference:System.Runtime.Serialization.dll `
    /reference:"$WebViewCore" /reference:"$WebViewForms" `
    /win32icon:"$Icon" /out:"$Output" "$Source"

if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Output)) {
    throw "Launcher compilation failed."
}

Copy-Item -LiteralPath $WebViewCore -Destination (Join-Path $ProjectRoot "Microsoft.Web.WebView2.Core.dll") -Force
Copy-Item -LiteralPath $WebViewForms -Destination (Join-Path $ProjectRoot "Microsoft.Web.WebView2.WinForms.dll") -Force
Copy-Item -LiteralPath $WebViewLoader -Destination (Join-Path $ProjectRoot "WebView2Loader.dll") -Force

if ($SigningCertificateThumbprint) {
    $normalizedThumbprint = ($SigningCertificateThumbprint -replace '\s', '').ToUpperInvariant()
    $certificate = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
        Where-Object { $_.Thumbprint.ToUpperInvariant() -eq $normalizedThumbprint } |
        Select-Object -First 1
    if (-not $certificate -or -not $certificate.HasPrivateKey) {
        throw "The requested code-signing certificate was not found with a private key in Cert:\CurrentUser\My."
    }
    $signature = Set-AuthenticodeSignature -LiteralPath $Output -Certificate $certificate `
        -HashAlgorithm SHA256 -TimestampServer $TimestampServer
    if ($signature.Status -ne "Valid") {
        throw "Launcher signing failed: $($signature.Status) - $($signature.StatusMessage)"
    }
    Write-Host "Signed $Output with $($certificate.Subject)"
} elseif ($RequireSigned) {
    throw "A trusted code-signing certificate is required. Set ELREN_SIGNING_CERT_THUMBPRINT or pass -SigningCertificateThumbprint."
} else {
    Write-Warning "Launcher built unsigned. A trusted Authenticode certificate is required to reduce SmartScreen and enterprise-policy warnings."
}

Write-Host "Built $Output"
