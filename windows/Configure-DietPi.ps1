<#
.SYNOPSIS
    Pre-configure dietpi.txt on a freshly flashed DietPi SD card (SETUP.md /
    SETUP.ja.md, Phase 0), so the manual copy/paste step can be automated.

.DESCRIPTION
    Edits dietpi.txt on the boot partition before the first boot: locale,
    keyboard layout, timezone, static network address, SWAP, and optionally
    the global password and the WiFi hotspot SSID/passphrase. Every value
    defaults to what SETUP.md documents; pass a parameter to override one.

    The script only touches the keys it knows about (each is replaced in
    place if present, active or commented out, appended otherwise) and
    leaves the rest of dietpi.txt untouched. A timestamped backup is written
    next to the file before anything is changed.

    All console output is English on purpose, matching every other script in
    this project (see CLAUDE.md): DietPi is meant to boot on a physical HDMI
    console or a bare serial terminal, and neither renders Japanese glyphs
    reliably. This is true even though the values being configured here -
    locale, timezone, keyboard layout - are mainly about running the Pi in a
    Japanese environment; only the Web UI (rendered in a browser) stays
    Japanese.

.PARAMETER DriveLetter
    Drive letter of the boot partition, e.g. "D". Auto-detected if omitted:
    the script scans mounted filesystem drives for a root-level dietpi.txt
    and fails if it finds none or more than one.

.PARAMETER StaticIP
    Static IPv4 address for the Pi. Default: 192.168.0.100 (SETUP.md's
    example). Adjust to your router's subnet if it isn't 192.168.0.x.

.PARAMETER StaticMask
    Static network mask. Default: 255.255.255.0.

.PARAMETER StaticGateway
    Default gateway / router address. Default: 192.168.0.1.

.PARAMETER StaticDNS
    DNS server(s) used only until Sentinel/AdGuard take over. Default:
    192.168.0.1. Space-separate multiple addresses if needed.

.PARAMETER Dhcp
    Skip the static address and leave AUTO_SETUP_NET_USESTATIC=0 (DHCP).
    Not recommended - see SETUP.md H1 for why a stable address matters.

.PARAMETER Timezone
    IANA timezone name. Default: Asia/Tokyo.

.PARAMETER Locale
    Console locale. Default: en_US.UTF-8. This is the boot console only;
    the Web UI is always Japanese regardless of this value.

.PARAMETER KeyboardLayout
    Console keyboard layout. Default: us.

.PARAMETER GlobalPassword
    Root/dietpi login password. It also becomes the AdGuard Home admin
    password once bootstrap.sh installs it. Left untouched if omitted, so
    DietPi's own default ("dietpi") applies - change it on first login.

.PARAMETER HotspotSsid
    Pre-set the WiFi hotspot SSID in dietpi.txt, so setup.sh's H2 step has
    nothing left to ask. Omit to decide interactively during H2 instead.

.PARAMETER HotspotPassphrase
    Pre-set the WiFi hotspot passphrase (8-63 characters, WPA2 requirement).
    Stored in clear text in dietpi.txt, same as DietPi does natively.

.EXAMPLE
    .\Configure-DietPi.ps1
    Auto-detects the boot drive and applies every SETUP.md default.

.EXAMPLE
    .\Configure-DietPi.ps1 -DriveLetter D -StaticIP 192.168.1.50 `
        -StaticGateway 192.168.1.1 -StaticDNS 192.168.1.1 `
        -HotspotSsid Sentinel -HotspotPassphrase "correct horse battery"
    Explicit drive and subnet, and pre-sets the hotspot too.

.EXAMPLE
    .\Configure-DietPi.ps1 -WhatIf
    Dry run: prints what would change, writes nothing (no backup either).

.NOTES
    Run this after flashing DietPi and before the first boot, then eject the
    card and continue with SETUP.md Phase 1. Requires PowerShell 5.1+
    (built into Windows 10/11); no extra install needed. No administrator
    rights are required - the boot partition is a normal removable FAT32
    volume writable by any user account that can see the drive letter.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$DriveLetter,
    [string]$StaticIP = '192.168.0.100',
    [string]$StaticMask = '255.255.255.0',
    [string]$StaticGateway = '192.168.0.1',
    [string]$StaticDNS = '192.168.0.1',
    [switch]$Dhcp,
    [string]$Timezone = 'Asia/Tokyo',
    [string]$Locale = 'en_US.UTF-8',
    [string]$KeyboardLayout = 'us',
    [string]$GlobalPassword,
    [string]$HotspotSsid,
    [string]$HotspotPassphrase
)

$ErrorActionPreference = 'Stop'

function Write-Step { param([string]$Message) Write-Host "== $Message ==" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Message) Write-Host "  [OK] $Message" -ForegroundColor Green }
function Write-Warn2 { param([string]$Message) Write-Host "  [!!] $Message" -ForegroundColor Yellow }
function Write-Fail {
    param([string]$Message)
    Write-Host "[FAIL] $Message" -ForegroundColor Red
    exit 1
}

function Test-IPv4 {
    param([string]$Value)
    $octet = '(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)'
    return $Value -match "^$octet(\.$octet){3}$"
}

# ------------------------------------------------------------------ locate
if (-not $DriveLetter) {
    Write-Step 'Locating the DietPi boot drive'
    $candidates = Get-PSDrive -PSProvider FileSystem | Where-Object {
        $_.Root -and (Test-Path -LiteralPath (Join-Path $_.Root 'dietpi.txt'))
    }
    if ($candidates.Count -eq 0) {
        Write-Fail 'No drive with dietpi.txt found. Flash DietPi first (the boot partition mounts automatically), or pass -DriveLetter explicitly.'
    }
    if ($candidates.Count -gt 1) {
        $names = ($candidates | ForEach-Object { $_.Name }) -join ', '
        Write-Fail "Multiple drives contain dietpi.txt: $names. Pass -DriveLetter to pick one."
    }
    $DriveLetter = $candidates[0].Name
    Write-Ok "Found the boot partition on drive ${DriveLetter}:"
}

$dietpiTxtPath = "${DriveLetter}:\dietpi.txt"
if (-not (Test-Path -LiteralPath $dietpiTxtPath)) {
    Write-Fail "dietpi.txt not found at $dietpiTxtPath"
}

# ------------------------------------------------------------------ validate
if (-not $Dhcp) {
    $addressChecks = @(
        @{ Name = 'StaticIP'; Value = $StaticIP }
        @{ Name = 'StaticMask'; Value = $StaticMask }
        @{ Name = 'StaticGateway'; Value = $StaticGateway }
    )
    foreach ($first in ($StaticDNS -split '\s+' | Where-Object { $_ })) {
        $addressChecks += @{ Name = 'StaticDNS'; Value = $first }
    }
    foreach ($check in $addressChecks) {
        if (-not (Test-IPv4 $check.Value)) {
            Write-Fail "-$($check.Name) '$($check.Value)' is not a valid IPv4 address."
        }
    }
}
if ($HotspotPassphrase -and ($HotspotPassphrase.Length -lt 8 -or $HotspotPassphrase.Length -gt 63)) {
    Write-Fail "-HotspotPassphrase must be 8-63 characters (WPA2 requirement); got $($HotspotPassphrase.Length)."
}
if ($HotspotPassphrase -and -not $HotspotSsid) {
    Write-Fail '-HotspotPassphrase was given without -HotspotSsid.'
}

# ------------------------------------------------------------------ backup
$backupPath = "$dietpiTxtPath.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
if ($PSCmdlet.ShouldProcess($dietpiTxtPath, 'create a timestamped backup')) {
    Write-Step 'Backing up dietpi.txt'
    Copy-Item -LiteralPath $dietpiTxtPath -Destination $backupPath
    Write-Ok "Backup written to $backupPath"
}

# ------------------------------------------------------------------ edit
# Read the raw text and keep the file's own line endings: DietPi parses this
# file on Linux with grep/sed, and turning LF into CRLF can leave a stray
# "\r" glued onto the end of a value.
$raw = [System.IO.File]::ReadAllText($dietpiTxtPath)
$eol = if ($raw -match "`r`n") { "`r`n" } else { "`n" }

function Set-DietPiValue {
    param(
        [Parameter(Mandatory)][ref]$Text,
        [Parameter(Mandatory)][string]$Key,
        [Parameter(Mandatory)][string]$Value,
        [Parameter(Mandatory)][string]$Eol
    )
    # Matches an active OR commented-out "KEY=..." line - the same
    # idempotent, replace-in-place-or-append approach the bash scripts use
    # with sed (bootstrap.sh, setup.sh). A MatchEvaluator (not a plain
    # replacement string) is used so a "$" in the value - a passphrase, say
    # - is never misread as a regex backreference.
    $pattern = "(?m)^[ \t]*#?[ \t]*$([regex]::Escape($Key))=.*$"
    $newLine = "$Key=$Value"
    $evaluator = [System.Text.RegularExpressions.MatchEvaluator] { param($m) $newLine }
    if ([regex]::IsMatch($Text.Value, $pattern)) {
        $Text.Value = [regex]::Replace($Text.Value, $pattern, $evaluator, 1)
    } else {
        $sep = if ($Text.Value.EndsWith($Eol)) { '' } else { $Eol }
        $Text.Value = $Text.Value + $sep + $newLine + $Eol
    }
}

Write-Step 'Applying settings'
$changes = [ordered]@{
    AUTO_SETUP_LOCALE               = $Locale
    AUTO_SETUP_KEYBOARD_LAYOUT      = $KeyboardLayout
    AUTO_SETUP_TIMEZONE             = $Timezone
    AUTO_SETUP_NET_ETHERNET_ENABLED = '1'
    AUTO_SETUP_SWAPFILE_SIZE        = '0'
    SURVEY_OPTED_IN                 = '0'
    CONFIG_CPU_GOVERNOR             = 'ondemand'
}
if ($Dhcp) {
    $changes['AUTO_SETUP_NET_USESTATIC'] = '0'
    Write-Warn2 '-Dhcp set: the static address block is skipped. SETUP.md recommends a static address (see H1 in setup.sh) because AdGuard and the hotspot both depend on this Pi keeping the same address.'
} else {
    $changes['AUTO_SETUP_NET_USESTATIC']      = '1'
    $changes['AUTO_SETUP_NET_STATIC_IP']      = $StaticIP
    $changes['AUTO_SETUP_NET_STATIC_MASK']    = $StaticMask
    $changes['AUTO_SETUP_NET_STATIC_GATEWAY'] = $StaticGateway
    $changes['AUTO_SETUP_NET_STATIC_DNS']     = $StaticDNS
}
if ($GlobalPassword) { $changes['AUTO_SETUP_GLOBAL_PASSWORD'] = $GlobalPassword }
if ($HotspotSsid) { $changes['SOFTWARE_WIFI_HOTSPOT_SSID'] = $HotspotSsid }
if ($HotspotPassphrase) { $changes['SOFTWARE_WIFI_HOTSPOT_KEY'] = $HotspotPassphrase }

$secretKeys = @('AUTO_SETUP_GLOBAL_PASSWORD', 'SOFTWARE_WIFI_HOTSPOT_KEY')
foreach ($key in $changes.Keys) {
    $displayValue = if ($key -in $secretKeys) { '********' } else { $changes[$key] }
    if ($PSCmdlet.ShouldProcess($dietpiTxtPath, "set $key=$displayValue")) {
        Set-DietPiValue -Text ([ref]$raw) -Key $key -Value $changes[$key] -Eol $eol
        Write-Ok "$key = $displayValue"
    }
}

if ($PSCmdlet.ShouldProcess($dietpiTxtPath, 'write the updated file')) {
    [System.IO.File]::WriteAllText($dietpiTxtPath, $raw, [System.Text.UTF8Encoding]::new($false))
    Write-Ok 'dietpi.txt updated.'
    Write-Step 'Next steps'
    Write-Host '  1. Safely eject the drive.'
    Write-Host '  2. Put the SD card in the Raspberry Pi and boot it with Ethernet connected.'
    Write-Host '  3. Continue with SETUP.md (or SETUP.ja.md) Phase 1.'
} else {
    Write-Warn2 "-WhatIf: nothing was written. The backup at $backupPath is unused and safe to delete."
}
