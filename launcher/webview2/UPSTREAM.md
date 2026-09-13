# WebView2 SDK provenance

These DLLs and the adjacent LICENSE.txt and NOTICE.txt come from Microsoft's
Microsoft.Web.WebView2 1.0.4129.50 NuGet package:

https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/1.0.4129.50/microsoft.web.webview2.1.0.4129.50.nupkg

On 2026-09-13, each repository DLL was matched byte-for-byte (SHA-256) to
the following upstream package entry:

| Entry | SHA-256 |
| --- | --- |
| lib/net462/Microsoft.Web.WebView2.Core.dll | 958efdb7f13a6d1f3079756c96956cc96cf713ae46fa085c8b1e7f44316a4f7e |
| lib/net462/Microsoft.Web.WebView2.WinForms.dll | a7b8be525030f19d9e88c6e684bca053dc7a3b080c31c3d9428f7438e7b6768f |
| build/native/x64/WebView2Loader.dll | a9a09232c25805323d4cfb3fc8f545a190a9c8a99c93262ea99d0b88df99ec90 |

Preserve both notices in Windows distributions. This documents the SDK
assemblies/loader, not the separate WebView2 browser Runtime. Runtime
redistribution must be reviewed for the exact Runtime payload separately.
