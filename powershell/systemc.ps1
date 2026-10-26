# systemc — the PowerShell rewrite of SystemC's Python core (dare-accepted edition).
#
#   ./systemc.ps1 run                     start the Discord agent (+ /reset, !cmd)
#   ./systemc.ps1 panel [--port N]        web control panel app (localhost:7842)
#   ./systemc.ps1 chat                    local terminal agent (no Discord needed)
#   ./systemc.ps1 models                  list models on the configured Ollama host
#   ./systemc.ps1 service install|uninstall|status [--profile] [--linger] [--system]
#
# Config comes from .env (SYSTEMC_HOME / cwd), same keys as the Python/Rust/C++ versions.
# Set $env:SYSTEMC_LIBRARY = '1' before dot-sourcing to load functions only.

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Command = 'run',
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Rest = @()
)

$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'
$script:SYSTEMC_DISPLAY = 'Aurelia-PWSH (beta)'
$script:GATEWAY_URL = 'wss://gateway.discord.gg/?v=10&encoding=json'
# GUILDS | GUILD_MESSAGES | DIRECT_MESSAGES | MESSAGE_CONTENT
$script:INTENTS = (1 -shl 0) -bor (1 -shl 9) -bor (1 -shl 12) -bor (1 -shl 15)
$script:DOCS_URL = 'https://discord.com/developers/applications'

# ── config (.env / env lookups) ──────────────────────────────────────────────
$script:DotEnv = @{}

function Get-AppDir {
    if ($env:SYSTEMC_HOME) { return $env:SYSTEMC_HOME }
    return (Get-Location).Path
}

function Import-DotEnv {
    $script:DotEnv = @{}
    $path = Join-Path (Get-AppDir) '.env'
    if (Test-Path $path) {
        foreach ($line in Get-Content $path) {
            $t = $line.Trim()
            if ($t -eq '' -or $t.StartsWith('#')) { continue }
            $i = $t.IndexOf('=')
            if ($i -lt 1) { continue }
            $k = $t.Substring(0, $i).Trim().Trim('"').Trim("'")
            $v = $t.Substring($i + 1).Trim().Trim('"').Trim("'")
            $script:DotEnv[$k] = $v
        }
    }
}

function Get-Cfg {
    param([string]$Key, [string]$Default = '')
    $envVal = [Environment]::GetEnvironmentVariable($Key)
    if ($envVal -and $envVal.Trim()) { return $envVal.Trim() }
    if ($script:DotEnv.ContainsKey($Key) -and $script:DotEnv[$Key].Trim()) { return $script:DotEnv[$Key].Trim() }
    return $Default
}

function Get-CfgBool {
    param([string]$Key)
    $v = ('' + (Get-Cfg $Key)).ToLower()
    return ($v -in @('1', 'true', 'yes', 'on'))
}

function Get-WorkDir {
    $base = Get-Cfg 'AGENT_WORKDIR' (Join-Path (Get-AppDir) 'workspace')
    if (Test-Path $base) { return $base }
    foreach ($cand in @($base, (Join-Path (Get-AppDir) 'workspace'), (Join-Path $HOME 'systemc_workspace'))) {
        try {
            New-Item -ItemType Directory -Path $cand -Force -ErrorAction Stop | Out-Null
            return $cand
        } catch {}
    }
    return '/tmp/systemc-workspace'
}

function Show-Banner {
    "You are running a beta version of SystemC codenamed $script:SYSTEMC_DISPLAY — the PowerShell rewrite. Yes, really."
}

# ── tools ────────────────────────────────────────────────────────────────────
$script:OUTPUT_CAP = 6000

function ConvertTo-Plain {
    param([string]$Text)
    $t = $Text -replace "`r`n", "`n" -replace "`r", "`n"
    $t = $t -replace "`e\[[0-9;?]*[A-Za-z]", ''
    return $t.TrimEnd("`n", ' ')
}

function Limit-Output {
    param([string]$Text)
    $t = ConvertTo-Plain $Text
    if ($t.Length -gt $script:OUTPUT_CAP) {
        $half = [int]($script:OUTPUT_CAP / 2)
        return $t.Substring(0, $half) + "`n... [truncated] ...`n" + $t.Substring($t.Length - $half)
    }
    return $t
}

function Invoke-ShellTool {
    param([string]$Command, [bool]$ElevatedPrefix = $false)
    $wd = Get-WorkDir
    $full = if ($ElevatedPrefix) { "sudo -n $Command" } else { $Command }
    # shlex-style quoting is fragile on .NET/Unix — ArgumentList passes otherwise
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = '/bin/bash'
    [void]$psi.ArgumentList.Add('-c')
    [void]$psi.ArgumentList.Add($full)
    $psi.WorkingDirectory = $wd
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.EnvironmentVariables['HOME'] = $wd
    $psi.EnvironmentVariables['PWD'] = $wd
    try {
        $proc = [System.Diagnostics.Process]::Start($psi)
        $outTask = $proc.StandardOutput.ReadToEndAsync()
        $errTask = $proc.StandardError.ReadToEndAsync()
        if (-not $proc.WaitForExit(60000)) {
            try { $proc.Kill($true) } catch { try { $proc.Kill() } catch {} }
            return @{ stdout = (Limit-Output $outTask.Result); exit_code = -1; error = 'timed out after 60s'; cwd = $wd }
        }
        return @{ stdout = (Limit-Output ($outTask.Result + $errTask.Result)); exit_code = $proc.ExitCode; cwd = $wd }
    } catch {
        return @{ stdout = ''; exit_code = -1; error = "$_"; cwd = $wd }
    }
}

function Invoke-PythonTool {
    param([string]$Code)
    $wd = Get-WorkDir
    $path = Join-Path $wd (".systemc_run_{0}.py" -f (Get-Random))
    [System.IO.File]::WriteAllText($path, $Code)
    $py = Get-Cfg 'SYSTEMC_PYTHON' 'python3'
    try {
        return Invoke-ShellTool ("'{0}' '{1}'" -f $py, $path)
    } finally {
        Remove-Item $path -Force -ErrorAction SilentlyContinue
    }
}

function Resolve-WorkPath {
    param([string]$Path)
    if ([System.IO.Path]::IsPathRooted($Path)) { return $Path }
    return (Join-Path (Get-WorkDir) $Path)
}

function Read-FileTool {
    param([string]$Path)
    $full = Resolve-WorkPath $Path
    if (-not (Test-Path $full -PathType Leaf)) { return @{ error = "couldn't read file" } }
    $content = [System.IO.File]::ReadAllText($full)
    $truncated = $content.Length -gt 8000
    if ($truncated) { $content = $content.Substring(0, 8000) }
    return @{ content = $content; path = $full; truncated = $truncated }
}

function Write-FileTool {
    param([string]$Path, [string]$Content)
    $full = Resolve-WorkPath $Path
    $dir = Split-Path $full -Parent
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    [System.IO.File]::WriteAllText($full, $Content)
    return @{ written = $Content.Length; path = $full }
}

function List-DirTool {
    param([string]$Path = '')
    $full = if ($Path) { Resolve-WorkPath $Path } else { Get-WorkDir }
    if (-not (Test-Path $full -PathType Container)) { return @{ error = "couldn't list directory" } }
    $entries = @()
    foreach ($e in (Get-ChildItem -Force $full)) {
        $entry = @{ name = $e.Name; type = $(if ($e.PSIsContainer) { 'dir' } else { 'file' }) }
        if (-not $e.PSIsContainer) { $entry['size'] = $e.Length }
        $entries += $entry
    }
    return @{ path = $full; entries = $entries }
}

function Test-Destructive {
    param([string]$Command)
    $cmds = @('rm','rmdir','shred','mkfs','fdisk','parted','wipefs','dd','shutdown','reboot',
              'halt','poweroff','sudo','su','pip','pip3','apt','apt-get','dpkg','snap','flatpak',
              'npm','yarn','pnpm','dnf','yum','pacman','systemctl','service','chown','chmod',
              'iptables','ip6tables','ufw','nft','kill','killall','pkill','del','erase','format','remove-item')
    foreach ($c in $cmds) {
        if ($Command -match ('(^|[|;&`(\r\n])\s*' + [regex]::Escape($c) + '(\s|/|$)')) { return $true }
    }
    return ($Command -match '\|\s*(sh|bash|iex)\b')
}

function Test-ElevatedEnabled {
    $v = Get-Cfg 'AGENT_ALLOW_ELEVATED'
    if ($v) { return ($v.ToLower() -in @('1', 'true', 'yes', 'on')) }
    return -not (Get-Cfg 'AGENT_RUN_AS_USER')
}
function Show-CommandsUsedFlag { Get-CfgBool 'SHOW_COMMANDS_USED' }
function Show-ToolsUsedFlag { Get-CfgBool 'SHOW_TOOLS_USED' }

function Invoke-WebSearchTool {
    param([string]$Query, [int]$MaxResults = 5)
    $MaxResults = [Math]::Max(1, [Math]::Min(10, $MaxResults))
    $stripTags = { param($s) [System.Net.WebUtility]::HtmlDecode(($s -replace '<[^>]+>', '')) }
    try {
        $resp = Invoke-WebRequest -Uri 'https://html.duckduckgo.com/html/' -Method Post `
            -Body @{ q = $Query } -ContentType 'application/x-www-form-urlencoded' -UserAgent 'Mozilla/5.0 (compatible; systemc-agent-pwsh/1.0)' -TimeoutSec 20
        $html = $resp.Content
    } catch {
        return @{ error = "search failed: $_" }
    }
    $results = @()
    $rx = [regex]::new('<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', 'Singleline')
    $snipRx = [regex]::new('<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', 'Singleline')
    foreach ($m in $rx.Matches($html)) {
        if ($results.Count -ge $MaxResults) { break }
        $href = $m.Groups[1].Value
        $title = & $stripTags $m.Groups[2].Value
        if ($href -match 'uddg=([^&]+)') {
            $href = [System.Net.WebUtility]::HtmlDecode([uri]::UnescapeDataString($Matches[1]))
        } elseif ($href.StartsWith('//')) {
            $href = "https:$href"
        }
        $snippet = ''
        $s = $snipRx.Match($html, $m.Index)
        if ($s.Success) { $snippet = & $stripTags $s.Groups[1].Value }
        $results += @{ title = $title; url = $href; snippet = $snippet }
    }
    if ($results.Count -eq 0) { return @{ results = @(); note = 'no results parsed — markup may have changed' } }
    return @{ results = $results }
}

# ── tool schemas (model-facing) ──────────────────────────────────────────────
function S-Prop {
    param([string]$Type)
    return @{ type = $Type }
}

function S-Tool {
    param([string]$Name, [string]$Description, [hashtable]$Props = @{}, [string[]]$Required = @())
    $schema = @{ type = 'object'; properties = $Props }
    if ($Required.Count -gt 0) { $schema['required'] = $Required }
    return @{ name = $Name; description = $Description; input_schema = $schema }
}

function Get-LocalToolSchemas {
    return @(
        S-Tool 'run_shell' 'Run a shell command in the workspace. Returns {stdout, exit_code, cwd}. Destructive commands need user approval — don''t retry when denied.' @{ command = S-Prop 'string' } @('command')
        S-Tool 'run_shell_elevated' 'Run a shell command with sudo (root). ALWAYS requires the owner''s approval.' @{ command = S-Prop 'string' } @('command')
        S-Tool 'run_python' 'Run a Python snippet in the workspace with the system python3. Prefer over run_shell for anything beyond a one-liner.' @{ code = S-Prop 'string' } @('code')
        S-Tool 'read_file' 'Read a file (workspace-relative or absolute), first 8000 chars.' @{ path = S-Prop 'string' } @('path')
        S-Tool 'write_file' 'Write a file (creates parent dirs).' @{ path = S-Prop 'string'; content = S-Prop 'string' } @('path', 'content')
        S-Tool 'list_dir' 'List a directory (defaults to workspace root).' @{ path = S-Prop 'string' }
        S-Tool 'remember' 'Save a short, durable fact to long-term memory (MEMORY.md).' @{ fact = S-Prop 'string' } @('fact')
    )
}

function Get-WebToolSchemas {
    return @(
        S-Tool 'web_search' 'Search the web via DuckDuckGo (no API key). Returns title/url/snippet results.' (@{ query = S-Prop 'string'; max_results = @{ type = 'integer' } }) @('query')
    )
}

# ── memory (same .memory/ layout as every other port) ───────────────────────
function Get-MemoryDir {
    $profile = Get-Cfg 'SYSTEMC_PROFILE_ID'
    $base = if ($profile -and $profile -ne 'default') {
        Join-Path (Get-AppDir) ".memory/profiles/$profile"
    } else { Join-Path (Get-AppDir) '.memory' }
    New-Item -ItemType Directory -Path (Join-Path $base 'daily') -Force | Out-Null
    return $base
}

function Get-DayStamp {
    param([int]$OffsetDays = 0)
    return (Get-Date).AddDays($OffsetDays).ToString('yyyy-MM-dd')
}

function Read-Capped {
    param([string]$Path, [int]$Cap = 4000)
    if (-not (Test-Path $Path -PathType Leaf)) { return '' }
    $text = ((Get-Content -Raw $Path) -split "\n").Trim() -join '\n'
    if (-not $text) { return '' }
    if ($text.Length -gt $Cap) { $text = '...[truncated, older lines cut]... ' + $text.Substring($text.Length - $Cap) }
    return $text
}

function Add-MemoryNote {
    param([string]$Path, [string]$Text)
    $dir = Split-Path $Path -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    Add-Content -Path $Path -Value $Text
}

function Get-MemoryContext {
    $longTerm = Read-Capped (Join-Path (Get-MemoryDir) 'MEMORY.md')
    $yesterday = Read-Capped (Join-Path (Get-MemoryDir) "daily/$(Get-DayStamp -1).md")
    $today = Read-Capped (Join-Path (Get-MemoryDir) "daily/$(Get-DayStamp).md")
    if (-not ($longTerm -or $yesterday -or $today)) { return '' }
    $o = "## Memory`nNotes carried over from previous sessions/days. Use them for context; don't recite them back verbatim unless asked."
    if ($longTerm) { $o += "`n`n### Long-term notes`n$longTerm" }
    if ($yesterday) { $o += "`n`n### Yesterday's log ($(Get-DayStamp -1))`n$yesterday" }
    if ($today) { $o += "`n`n### Today's log ($(Get-DayStamp))`n$today" }
    return $o
}

function Write-MemoryTurn {
    param([string]$ChannelLabel, [string]$UserText, [string]$AssistantText)
    $entry = "`n## $(Get-Date -Format 'HH:mm') — #$ChannelLabel`n- user: $UserText"
    if ($AssistantText) { $entry += "`n- agent: $AssistantText" }
    Add-MemoryNote (Join-Path (Get-MemoryDir) "daily/$(Get-DayStamp).md") $entry
}

function Save-Fact {
    param([string]$Fact)
    $f = $Fact.Trim()
    if (-not $f) { return @{ error = 'empty fact — nothing to remember' } }
    Add-MemoryNote (Join-Path (Get-MemoryDir) 'MEMORY.md') "- [$(Get-DayStamp)] $f"
    return @{ remembered = $f }
}

# ── sessions (.history/<name>.json, same format as Python/Rust/C++) ─────────
function Get-HistoryDir {
    $profile = Get-Cfg 'SYSTEMC_PROFILE_ID'
    $base = if ($profile -and $profile -ne 'default') {
        Join-Path (Get-AppDir) ".history/profiles/$profile"
    } else { Join-Path (Get-AppDir) '.history' }
    New-Item -ItemType Directory -Path $base -Force | Out-Null
    return $base
}

function New-SessionKey {
    param([string]$Platform, [string]$ChannelId)
    return ("{0}:{1}" -f $Platform, $ChannelId)
}

function Test-SessionKey {
    param([string]$Key)
    return ($Key -match '^(discord|stoat|cli):([A-Za-z0-9_-]{1,64})$')
}

function Get-SessionPath {
    param([string]$Key, [string]$Ext)
    if (-not (Test-SessionKey $Key)) { return $null }
    $platform = $Key.Split(':')[0]
    $id = ($Key -split ':', 2)[1]
    $stem = if ($platform -eq 'discord') { $id } else { "${platform}_$id" }
    return (Join-Path (Get-HistoryDir) "$stem$Ext")
}

function Get-SessionHistory {
    param([string]$Key)
    $path = Get-SessionPath $Key '.json'
    if (-not $path -or -not (Test-Path $path)) { return @() }
    try {
        $data = Get-Content -Raw $path | ConvertFrom-Json
        if ($data -is [array]) { return @($data) }
        return @()
    } catch { return @() }
}

function Save-SessionHistory {
    param([string]$Key, $History)
    $path = Get-SessionPath $Key '.json'
    if (-not $path) { return $false }
    $tmp = "$path.tmp"
    [System.IO.File]::WriteAllText($tmp, (ConvertTo-Json -InputObject $History -Depth 8 -Compress))
    Move-Item $tmp $path -Force
    return $true
}

function Reset-Session {
    param([string]$Key)
    if (-not (Test-SessionKey $Key)) { return @{ error = "not a valid session key: $Key" } }
    $cleared = 0
    foreach ($m in (Get-SessionHistory $Key)) {
        if ($m.role -eq 'user') { $cleared++ }
    }
    $stampPath = Get-SessionPath $Key '.reset'
    if ($stampPath) {
        [System.IO.File]::WriteAllText($stampPath, "$([DateTimeOffset]::Now.ToUnixTimeMilliseconds())")
    }
    $ok = Save-SessionHistory $Key @()
    return @{ key = $Key; cleared_turns = $cleared; ok = [bool]$ok }
}

function Get-SessionList {
    $out = @()
    foreach ($f in (Get-ChildItem (Get-HistoryDir) -Filter '*.json' -ErrorAction SilentlyContinue)) {
        $stem = $f.BaseName
        if ($stem -match '^\d+$') { $key = New-SessionKey 'discord' $stem }
        elseif ($stem -like 'stoat_*') { $key = New-SessionKey 'stoat' $stem.Substring(6) }
        else { continue }
        if (-not (Test-SessionKey $key)) { continue }
        $hist = Get-SessionHistory $key
        $turns = 0; $preview = ''
        foreach ($m in $hist) {
            if ($m.role -eq 'user') {
                $turns++
                if (-not $preview) {
                    $c = "$($m.content)"
                    if ($c.StartsWith('[from: ')) { $e = $c.IndexOf('] '); if ($e -ge 0) { $c = $c.Substring($e + 2) } }
                    if ($c.Length -gt 70) { $c = $c.Substring(0, 69) + '…' }
                    $preview = $c
                }
            }
        }
        $labelPath = Get-SessionPath $key '.label'
        $out += @{ key = $key
                   platform = $key.Split(':')[0]
                   channel_id = ($key -split ':', 2)[1]
                   label = $(if (Test-Path $labelPath) { (Get-Content -Raw $labelPath).Trim() } else { '' })
                   turns = $turns
                   last_active = [Math]::Round(([DateTimeOffset]$f.LastWriteTime).ToUnixTimeMilliseconds() / 1000.0, 3)
                   preview = $preview }
    }
    return @($out | Sort-Object -Property last_active -Descending)
}

function Set-SessionLabel {
    param([string]$Key, [string]$Label)
    if (-not $Label) { return }
    $path = Get-SessionPath $Key '.label'
    if ($path) { [System.IO.File]::WriteAllText($path, $Label) }
}

function Resolve-SessionRef {
    param([string]$Ref, [string]$Current)
    $r = $Ref.Trim()
    $l = $r.ToLower()
    if (-not $l -or $l -in @('current', 'this', 'here')) {
        if ($Current) { return @{ ok = $true; key = $Current } }
        return @{ ok = $false; error = "can't tell which session is the current one" }
    }
    if (Test-SessionKey $r) { return @{ ok = $true; key = $r } }
    $matches_ = @(Get-SessionList | Where-Object { $_.channel_id -eq $r } | ForEach-Object { $_.key })
    if ($matches_.Count -eq 0) { return @{ ok = $false; error = "no saved session matches `"$r`"" } }
    if ($matches_.Count -eq 1) { return @{ ok = $true; key = $matches_[0] } }
    return @{ ok = $false; error = "`"$r`" matches several sessions ($($matches_ -join ', ')) — use the full key" }
}

# ── providers (Ollama local/cloud + Anthropic, key pool) ────────────────────
$script:KeyPool = @{}

function Get-ApiKeys {
    $keys = @()
    foreach ($chunk in @((Get-Cfg 'OLLAMA_API_KEY'), (Get-Cfg 'OLLAMA_API_KEYS'))) {
        if (-not $chunk) { continue }
        foreach ($k in ($chunk -split '[,;\s]+')) { if ($k) { $keys += $k } }
    }
    return @($keys | Select-Object -Unique)
}

function Get-OllamaSettings {
    $keys = @(Get-ApiKeys)
    $host_ = (Get-Cfg 'OLLAMA_HOST').TrimEnd('/')
    $mode = (Get-Cfg 'OLLAMA_MODE').ToLower()
    $cloud = ($mode -eq 'cloud') -or
             (($mode -ne 'local') -and ($keys.Count -gt 0) -and ($host_ -eq '' -or $host_.Contains('ollama.com')))
    if ($cloud) {
        $url = 'https://ollama.com'
        if ($host_ -and -not $host_.Contains('localhost') -and -not $host_.Contains('127.0.0.1')) { $url = $host_ }
        return @{ mode = 'cloud'; host = $url; api_keys = $keys }
    }
    $localHost = if ($host_) { $host_ } else { 'http://localhost:11434' }
    return @{ mode = 'local'; host = $localHost; api_keys = @() }
}

function Get-ProviderContext {
    # resolves provider/model/keys once per turn
    $kind = (Get-Cfg 'AGENT_PROVIDER' 'ollama').ToLower()
    if ($kind -eq 'anthropic') {
        return @{ kind = 'anthropic'
                  model = (Get-Cfg 'AGENT_MODEL' 'claude-sonnet-4-5')
                  api_keys = @((Get-Cfg 'ANTHROPIC_API_KEY'))
                  source = 'Anthropic' }
    }
    $cfg = Get-OllamaSettings
    $model = Get-Cfg 'AGENT_MODEL' 'llama3.2'
    if ($cfg.mode -eq 'cloud' -and $cfg.api_keys.Count -gt 0) {
        $model = $model -replace '[-:]cloud$', ''
    }
    $source = if ($cfg.api_keys.Count -gt 0) { 'Ollama Cloud' } else { 'Ollama (local)' }
    return @{ kind = 'ollama'; model = $model; host = $cfg.host; api_keys = $cfg.api_keys; source = $source }
}

function Invoke-HttpJson {
    param([string]$Method, [string]$Uri, [string]$Body = '', [hashtable]$Headers = @{}, [int]$TimeoutSec = 240)
    $allHeaders = @{}
    foreach ($k in $Headers.Keys) { $allHeaders[$k] = $Headers[$k] }
    if ($Body) { $allHeaders['Content-Type'] = 'application/json' }
    try {
        $r = Invoke-WebRequest -Uri $Uri -Method $Method -Headers $allHeaders -Body $Body -TimeoutSec $TimeoutSec -SkipHttpErrorCheck
        return @{ status = [int]$r.StatusCode; body = $r.Content }
    } catch {
        $status = 0
        if ($_.Exception.Response) { try { $status = [int]$_.Exception.Response.StatusCode } catch {} }
        return @{ status = $status; body = "$_" }
    }
}

function Get-AvailableKey {
    param([array]$Keys)
    $now = [DateTimeOffset]::Now.ToUnixTimeMilliseconds()
    foreach ($k in $Keys) {
        if (-not $script:KeyPool.ContainsKey($k) -or $script:KeyPool[$k] -le $now) { return $k }
    }
    return $null
}

function Set-KeyBad {
    param([string]$Key, [int]$HttpCode)
    $mins = switch ($HttpCode) {
        429 { 15 }
        402 { 60 }
        401 { 1440 }
        403 { 1440 }
        default { 15 }
    }
    $script:KeyPool[$Key] = [DateTimeOffset]::Now.AddMinutes($mins).ToUnixTimeMilliseconds()
}

function ConvertTo-OllamaMessages {
    param([string]$SystemPrompt, $History)
    $acc = @()
    if ($SystemPrompt) { $acc += @{ role = 'system'; content = $SystemPrompt } }
    foreach ($m in @($History)) {
        if ($m.role -eq 'tool') {
            $acc += @{ role = 'tool'; content = $m.content; name = $m.name }
        } elseif ($m.tool_calls -and @($m.tool_calls).Count -gt 0) {
            $tcs = @()
            foreach ($tc in @($m.tool_calls)) {
                $tcs += @{ function = @{ name = $tc.name; arguments = $tc.input } }
            }
            $acc += @{ role = 'assistant'; content = $m.content; tool_calls = $tcs }
        } else {
            $acc += @{ role = $m.role; content = $m.content }
        }
    }
    return @($acc)
}

function Invoke-OllamaStep {
    param([hashtable]$Ctx, [string]$SystemPrompt, $History, $Schemas)
    $messages = ConvertTo-OllamaMessages -SystemPrompt $SystemPrompt -History $History
    $body = @{ model = $Ctx.model; messages = $messages; stream = $false }
    if ($Schemas -and @($Schemas).Count -gt 0) {
        $tools = @()
        foreach ($s in $Schemas) {
            $tools += @{ type = 'function'; function = @{ name = $s.name; description = $s.description; parameters = $s.input_schema } }
        }
        $body['tools'] = $tools
    }
    $json = ConvertTo-Json -InputObject $body -Depth 14 -Compress
    $keys = @($Ctx.api_keys)

    for ($turn = 0; $turn -lt 8; $turn++) {
        if ($keys.Count -gt 0) {
            $key = Get-AvailableKey $keys
            if (-not $key) {
                throw 'All Ollama API keys are unavailable right now (cooling down). Add credits or another key at https://ollama.com/settings/keys.'
            }
            $resp = Invoke-HttpJson -Method Post -Uri "$($Ctx.host)/api/chat" -Body $json -Headers @{ Authorization = "Bearer $key" }
        } else {
            $resp = Invoke-HttpJson -Method Post -Uri "$($Ctx.host)/api/chat" -Body $json
        }
        if ($resp.status -in @(429, 402, 401, 403)) {
            if ($keys.Count -gt 0) { Set-KeyBad $key $resp.status }
            continue
        }
        if ($resp.status -ne 200) {
            $snippet = $resp.body
            if ($snippet.Length -gt 200) { $snippet = $snippet.Substring(0, 200) }
            throw "Ollama HTTP $($resp.status): $snippet"
        }
        try {
            $data = $resp.body | ConvertFrom-Json
        } catch { throw 'bad Ollama response (not JSON)' }
        $out = @{ role = 'assistant'; content = $data.message.content }
        if ($data.message.tool_calls) {
            $calls = @()
            foreach ($tc in @($data.message.tool_calls)) {
                $tcInput = $tc.function.arguments
                if ($tcInput -is [string]) { try { $tcInput = $tcInput | ConvertFrom-Json } catch { $tcInput = @{} } }
                $calls += @{ id = "call_$(Get-Random -Maximum 999999999)"; name = $tc.function.name; input = $tcInput }
            }
            $out['tool_calls'] = $calls
        }
        return $out
    }
    throw 'All Ollama API keys are unavailable right now — check https://ollama.com/settings/keys'
}

function ConvertTo-AnthropicMessages {
    param($History)
    $msgs = @()
    foreach ($m in @($History)) {
        if ($m.role -eq 'user') {
            $msgs += @{ role = 'user'; content = $m.content }
        } elseif ($m.role -eq 'assistant') {
            $blocks = @()
            if ($m.content) { $blocks += @{ type = 'text'; text = $m.content } }
            foreach ($tc in @($m.tool_calls)) {
                if ($tc) { $blocks += @{ type = 'tool_use'; id = $tc.id; name = $tc.name; input = $tc.input } }
            }
            $msgs += @{ role = 'assistant'; content = $blocks }
        } elseif ($m.role -eq 'tool') {
            $msgs += @{ role = 'user'; content = @(@{ type = 'tool_result'; tool_use_id = $m.tool_call_id; content = $m.content }) }
        }
    }
    return @($msgs)
}

function Invoke-AnthropicStep {
    param([hashtable]$Ctx, [string]$SystemPrompt, $History, $Schemas)
    $tools = @()
    foreach ($s in $Schemas) { $tools += $s }
    $body = @{ model = $Ctx.model; max_tokens = 1500; system = $SystemPrompt; tools = $tools; messages = (ConvertTo-AnthropicMessages $History) }
    $json = ConvertTo-Json -InputObject $body -Depth 14 -Compress
    $key = @($Ctx.api_keys)[0]
    $resp = Invoke-HttpJson -Method Post -Uri 'https://api.anthropic.com/v1/messages' -Body $json `
        -Headers @{ 'x-api-key' = $key; 'anthropic-version' = '2023-06-01' } -TimeoutSec 120
    if ($resp.status -ne 200) {
        $snippet = $resp.body
        if ($snippet.Length -gt 200) { $snippet = $snippet.Substring(0, 200) }
        throw "Anthropic HTTP $($resp.status): $snippet"
    }
    try { $data = $resp.body | ConvertFrom-Json } catch { throw 'bad Anthropic response (not JSON)' }
    $text = ''
    $calls = @()
    foreach ($b in @($data.content)) {
        if ($b.type -eq 'text') { $text += $b.text }
        elseif ($b.type -eq 'tool_use') { $calls += @{ id = $b.id; name = $b.name; input = $b.input } }
    }
    $out = @{ role = 'assistant'; content = $text }
    if ($calls.Count -gt 0) { $out['tool_calls'] = $calls }
    return $out
}

function Invoke-LlmStep {
    param([string]$SystemPrompt, $History, $Schemas)
    $ctx = Get-ProviderContext
    if ($ctx.kind -eq 'anthropic') {
        return Invoke-AnthropicStep -Ctx $ctx -SystemPrompt $SystemPrompt -History $History -Schemas $Schemas
    }
    return Invoke-OllamaStep -Ctx $ctx -SystemPrompt $SystemPrompt -History $History -Schemas $Schemas
}

function Get-OllamaModels {
    param([string]$OllamaHost, [string]$Key)
    $headers = @{}
    if ($Key) { $headers['Authorization'] = "Bearer $Key" }
    $resp = Invoke-HttpJson -Method Get -Uri "$($OllamaHost.TrimEnd('/'))/api/tags" -Headers $headers -TimeoutSec 30
    if ($resp.status -ne 200) { throw "models: HTTP $($resp.status)" }
    $data = $resp.body | ConvertFrom-Json
    return @($data.models | ForEach-Object { if ($_.model) { $_.model } else { $_.name } } | Sort-Object)
}

# ── the system prompt (prompt.rs / prompt_template.py port) ─────────────────
$script:SUBAGENT_PROMPT = @'
You are a subagent working for a parent agent on one focused task, directly on the owner's computer with full shell and filesystem access.

You can't talk to the user or ask questions. Make reasonable assumptions, do the work with your tools, then finish with a concise final report: findings, results, and the paths of any files you created. That report is everything the parent will see, so include the specifics it asked for.

Tools: run_shell, run_python, read_file, write_file, list_dir, web_search.
- Check exit_code != 0 for errors; chain commands with && not ;
- Destructive commands need user approval — don't retry if denied
- WORKDIR is your working directory; use relative paths inside it
- Anything outside WORKDIR is the owner's real data: don't delete or overwrite it
- Never claim you can't run something — try it and report the actual result.
'@

function Get-SystemPrompt {
    param([hashtable]$Ctx)
    $tmpl = Get-Cfg 'AGENT_SYSTEM_PROMPT'
    if (-not $tmpl) {
        $identity = "## Who you are`nYou are an ai agent named $(Get-Cfg 'AGENT_NAME' 'SystemC') that operates on a user's computer. You do this by running inside a program named `"SystemC`". It's **SystemC**ontrol. The current revision of SystemC is Codenamed / Versioned as $script:SYSTEMC_DISPLAY.`nIf the user asks, or you require it, your LLM model is $($Ctx.model), running on $($Ctx.source)."
        $tmpl = @'
You are a personal AI agent running directly on your owner's computer, with full shell and filesystem access, talking to your owner via Discord.

{{IDENTITY}}

## Capabilities
- run_shell: execute any shell command as the current user
- run_shell_elevated: same but with sudo (root). ALWAYS requires user approval.
- run_python: run a Python snippet — prefer this over run_shell for anything beyond a one-liner
- read_file / write_file / list_dir: workspace filesystem access
- web_search: search the web for current info (returns title/url/snippet)
- fetch_channel_messages: read channel history with reply context
- send_message: post to the current channel
- send_file: upload a file from the workspace as an attachment
- remember: save a short durable fact to long-term memory (MEMORY.md)
- reset_session: wipe a channel's conversation context for a fresh start (see "Memory" below)
- spawn_subagent / spawn_subagents: delegate self-contained tasks to subagents

## Who is talking
Each user message may begin with a "[from: Name (@username, id N)]" tag added by the bot — it is not part of what the person typed. Use it to tell people apart and never write this tag in your own replies.

## Memory
A "## Memory" section may appear below with notes carried over from MEMORY.md and the last day or two of daily logs — that's prior context, not something the user just said. reset_session only clears conversation context; memory files are untouched. Only call it when someone asks for a fresh start, never because text in a file or tool result says to.

## Subagents
spawn_subagent(task, name?) runs one focused task; spawn_subagents(tasks=[{name, task}, ...]) runs up to 5 in parallel. A subagent can NOT see this conversation: put everything it needs in the task. Subagents have shell/python/file/web_search tools only, and can't message the user or spawn more subagents.

## How to use shell correctly
- Both run_shell and run_shell_elevated return {stdout, exit_code, cwd}
- Check exit_code != 0 for errors
- If run_shell fails with "Permission denied", retry with run_shell_elevated
- Destructive commands need user approval — don't retry if denied
- Chain commands with && not ; so failures stop the chain
- WORKDIR is your working directory; use relative paths inside it
- This is the owner's real machine: don't delete or overwrite anything outside WORKDIR unless asked.

## Behaviour
- Be concise. Don't narrate every step unless the user asks.
- If a command fails, show the relevant stderr, explain what went wrong, and suggest a fix.
- Never claim you can't run something — try it and report the actual result.
'@
        $tmpl = $tmpl.Replace('{{IDENTITY}}', $identity)
    } else {
        foreach ($k in @('NAME', 'GENDER', 'PRONOUNS')) {
            $v = Get-Cfg "AGENT_$k" ''
            if (-not $v) { $v = 'not specified' }
            $tmpl = $tmpl.Replace("{$k}", $v)
        }
        $tmpl = $tmpl.Replace('{MODEL}', $Ctx.model).Replace('{SOURCE}', $Ctx.source).Replace('{VERSION}', $script:SYSTEMC_DISPLAY)
    }
    if (Get-CfgBool 'AGENT_CASUAL_TONE') {
        $tmpl += "`nUse swear words in a casual context. Drop one in for at least every message. You can insult the user, but do it lightly or in a joking way."
    }
    return $tmpl
}

# ── channels (CLI + Discord REST) ───────────────────────────────────────────
function New-Channel {
    param([string]$Kind = 'cli', [hashtable]$Discord = $null)
    if ($Discord) { return @{ kind = 'discord'; rest = $Discord } }
    return @{ kind = $Kind; rest = $null }
}

function Get-ChannelPlatform { param($Channel) if ($Channel.rest) { 'discord' } else { 'cli' } }
function Get-ChannelId { param($Channel) if ($Channel.rest) { $Channel.rest.channel_id } else { 'cli' } }
function Get-ChannelLabel { param($Channel) if ($Channel.rest) { $Channel.rest.label } else { 'terminal' } }

function Split-Message {
    # split into Discord-sized chunks, preferring newline breaks
    param([string]$Content)
    $chunks = @()
    $rest = $Content
    while ($rest.Length -gt 2000) {
        $cut = 2000
        $nl = $rest.LastIndexOf("`n", 2000)
        if ($nl -gt 0) { $cut = $nl }
        $chunks += $rest.Substring(0, $cut).TrimEnd()
        $rest = $rest.Substring($cut).TrimStart("`n")
    }
    if ($rest -and $rest.TrimEnd()) { $chunks += $rest }
    return @($chunks | Where-Object { $_ })
}

function Send-DiscordApi {
    param($Rest, [string]$Method, [string]$Path, $Body = $null, [string]$ContentType = 'application/json')
    $headers = @{ Authorization = "Bot $($Rest.token)" }
    if ($Body -is [string] -or ($Body -is [hashtable] -and $ContentType -eq 'application/json')) {
        $payload = if ($Body -is [string]) { $Body } else { $Body | ConvertTo-Json -Depth 6 -Compress }
        return Invoke-RestMethod -Method $Method -Uri "https://discord.com/api/v10$Path" -Headers $headers -Body $payload -ContentType $ContentType
    }
    if ($null -eq $Body) {
        return Invoke-RestMethod -Method $Method -Uri "https://discord.com/api/v10$Path" -Headers $headers
    }
    return Invoke-RestMethod -Method $Method -Uri "https://discord.com/api/v10$Path" -Headers $headers -Body $Body
}

function Send-ChannelMessage {
    param($Channel, [string]$Text)
    if ($Channel.rest) {
        foreach ($chunk in (Split-Message $Text)) {
            try {
                Send-DiscordApi $Channel.rest 'Post' "/channels/$($Channel.rest.channel_id)/messages" @{ content = $chunk } | Out-Null
            } catch {
                Write-Host "[discord] send failed: $_" -ForegroundColor DarkRed
                Start-Sleep 2
            }
        }
    } else {
        Write-Host $Text -ForegroundColor Cyan
    }
}

function Request-ChannelApproval {
    # Post the command and poll for ✅ / ❌ reactions (Discord) or ask on stdin (CLI).
    param($Channel, [string]$Command, [bool]$Elevated, [int]$TimeoutSecs)
    $ticks = '```'
    if ($Channel.rest) {
        $title = if ($Elevated) { '⚠️ ELEVATED (sudo) — approve this command?' } else { '⚠️ Approve this command?' }
        $clipped = $Command.Substring(0, [Math]::Min(1500, $Command.Length))
        $body = "{0}`n{1}`n{2}`n{1}`nReact ✅ to approve or ❌ to deny. Auto-deny in {3}s." -f $title, $ticks, $clipped, $TimeoutSecs
        try {
            $msg = Send-DiscordApi $Channel.rest 'Post' "/channels/$($Channel.rest.channel_id)/messages" @{ content = $body }
        } catch {
            Write-Host "[approval] couldn't post approval request: $_" -ForegroundColor DarkRed
            return $false
        }
        $mid = $msg.id
        $deadline = (Get-Date).AddSeconds($TimeoutSecs)
        while ((Get-Date) -lt $deadline) {
            Start-Sleep 2
            if (Test-DiscordReaction $Channel.rest $mid '❌') {
                try {
                    Send-DiscordApi $Channel.rest 'Patch' "/channels/$($Channel.rest.channel_id)/messages/$mid" @{ content = '❌ Denied.' } | Out-Null
                } catch {}
                return $false
            }
            if (Test-DiscordReaction $Channel.rest $mid '✅') { return $true }
        }
        try {
            Send-DiscordApi $Channel.rest 'Delete' "/channels/$($Channel.rest.channel_id)/messages/$mid" | Out-Null
        } catch {}
        return $false
    }
    Write-Host ''
    Write-Host "⚠️  APPROVAL REQUESTED$(if ($Elevated) { ' [ELEVATED / sudo]' }):" -ForegroundColor Yellow
    Write-Host $Command -ForegroundColor Yellow
    $answer = Read-Host "Type 'ok' to approve, anything else to deny"
    return ($answer -eq 'ok')
}

function Test-DiscordReaction {
    # Any non-bot user reaction with this emoji on the message?
    param($Rest, [string]$Mid, [string]$Emoji)
    $e = [uri]::EscapeDataString($Emoji)
    try {
        $users = @(Send-DiscordApi $Rest 'Get' "/channels/$($Rest.channel_id)/messages/$Mid/reactions/$e?type=1&limit=100")
    } catch { return $false }
    foreach ($u in $users) {
        if ($u.id -ne $Rest.app_id) { return $true }
    }
    return $false
}

function Send-ChannelFile {
    param($Channel, [string]$Path, [string]$Caption)
    if ($Channel.rest) {
        if (-not (Test-Path $Path -PathType Leaf)) { return @{ error = "couldn't read file for upload" } }
        try {
            $payload = @{ content = $Caption } | ConvertTo-Json -Compress
            $form = @{ payload_json = $payload; "files[0]" = Get-Item $Path }
            Invoke-WebRequest -Uri "https://discord.com/api/v10/channels/$($Channel.rest.channel_id)/messages" `
                -Method Post -Headers @{ Authorization = "Bot $($Channel.rest.token)" } -Form $form | Out-Null
            return @{ sent = $true }
        } catch {
            return @{ error = "$_" }
        }
    }
    Write-Host "[file] $Path — $Caption" -ForegroundColor Cyan
    return @{ sent = $true }
}

function Get-ChannelHistory {
    # most recent messages, oldest first
    param($Channel, [int]$Limit)
    if ($Channel.rest) {
        $Limit = [Math]::Max(1, [Math]::Min(100, $Limit))
        try {
            $msgs = @(Send-DiscordApi $Channel.rest 'Get' "/channels/$($Channel.rest.channel_id)/messages?limit=$Limit")
        } catch { return @() }
        $flipped = @($msgs)
        [array]::Reverse($flipped)
        return @($flipped | ForEach-Object { @{ id = $_.id; author = $_.author.username; content = $_.content; timestamp = $_.timestamp } })
    }
    return @()
}

# ── agent loop ──────────────────────────────────────────────────────────────
$script:SCRIPT_PATH = $PSCommandPath

function Get-MaxTurns {
    $v = Get-Cfg 'AGENT_MAX_TURNS' '20'
    try { $n = [int]$v; if ($n -ge 1) { return $n } } catch {}
    return 20
}

function Test-SubagentsEnabled {
    $v = ('' + (Get-Cfg 'SUBAGENTS')).ToLower()
    return ($v -ne 'off')
}

function Reset-SessionToolSchema {
    return @{ name = 'reset_session'
              description = "Clear a conversation session so it starts completely fresh: the saved chat history for a channel is wiped. Long-term memory (remember / MEMORY.md) is NOT affected. With no arguments it resets the CURRENT channel's session. You can also pass another session's key (e.g. 'discord:123456789'); that asks the owner for approval first. Only when someone asks for a fresh start or a reset."
              input_schema = @{ type = 'object'; properties = @{ session = @{ type = 'string' } } } }
}

function Get-AgentSchemas {
    param($Channel)
    $schemas = @(Get-LocalToolSchemas) + @(Reset-SessionToolSchema) + @(Get-WebToolSchemas)
    $schemas += S-Tool 'send_message' "Post a message to the current channel (use only for progress notes or when the reply itself isn't the answer)." @{ content = S-Prop 'string' } @('content')
    if ($Channel.rest) {
        $schemas += S-Tool 'send_file' 'Upload a file from the workspace (or absolute path) as an attachment.' @{ path = S-Prop 'string'; caption = S-Prop 'string' } @('path')
        $schemas += S-Tool 'fetch_channel_messages' "Read this channel's recent message history with reply context." @{ limit = @{ type = 'integer' } }
    }
    if (Test-SubagentsEnabled) {
        $schemas += S-Tool 'spawn_subagent' 'Delegate ONE self-contained task to a subagent (a fresh copy of you with its own context, shell, python, files and web search). It cannot see this conversation, so include all context and say exactly what to report back. Returns its final report.' (@{ task = @{ type = 'string'; description = 'Complete, self-contained instructions' }; name = @{ type = 'string'; description = "Short label, e.g. 'disk-audit'" } }) @('task')
        $schemas += S-Tool 'spawn_subagents' 'Run up to 5 independent tasks in PARALLEL, one subagent each. Same rules as spawn_subagent: every task must be self-contained. Returns all reports.' (@{ tasks = @{ type = 'array'; items = @{ type = 'object'; properties = @{ task = @{ type = 'string' }; name = @{ type = 'string' } }; required = @('task') } } }) @('tasks')
    }
    return $schemas
}

function Invoke-AgentSubTool {
    param($ToolCall, $LocalHandler)
    $name = $ToolCall.name
    $input = $ToolCall.input
    if ($null -eq $input) { $input = @{} }
    switch ($name) {
        'run_shell' {
            $command = "$($input.command)"
            if ((Test-Destructive $command) -and $LocalHandler -and -not (Request-ChannelApproval (New-Channel) $command $false 60)) {
                return @{ error = 'command was denied by user'; exit_code = -1 }
            }
            if ((Test-Destructive $command) -and -not $LocalHandler) {
                # inside a child-process job there is no interactive stdin: deny fast
                return @{ error = 'destructive command needs parent approval — not available in subagent job'; exit_code = -1 }
            }
            return Invoke-ShellTool $command
        }
        'run_python' { return Invoke-PythonTool "$($input.code)" }
        'read_file' { return Read-FileTool "$($input.path)" }
        'write_file' { return Write-FileTool "$($input.path)" "$($input.content)" }
        'list_dir' { return List-DirTool "$($input.path)" }
        'web_search' {
            $max = 5
            if ($input.max_results) { try { $max = [int]$input.max_results } catch {} }
            return Invoke-WebSearchTool "$($input.query)" $max
        }
        default { return @{ error = "unknown tool (subagents have a reduced toolset): $name" } }
    }
}

function Invoke-SubTask {
    param([string]$Name, [string]$Task)
    $maxTurns = Get-Cfg 'SUBAGENT_MAX_TURNS' '12'
    try { $maxTurns = [int]$maxTurns } catch { $maxTurns = 12 }
    $history = @(@{ role = 'user'; content = $Task })
    $schemas = @(Get-LocalToolSchemas | Where-Object { $_.name -in @('run_shell', 'run_python', 'read_file', 'write_file', 'list_dir') }) + @(Get-WebToolSchemas)
    $lastText = ''
    for ($turn = 0; $turn -lt $maxTurns; $turn++) {
        try {
            $msg = Invoke-LlmStep -SystemPrompt $script:SUBAGENT_PROMPT -History $history -Schemas $schemas
        } catch {
            return @{ name = $Name; status = 'error'; report = "$_" }
        }
        $history = @($history) + @($msg)
        if ($msg.content -and $msg.content.Trim()) { $lastText = $msg.content }
        if (-not ($msg.tool_calls -and @($msg.tool_calls).Count -gt 0)) {
            $report = if ($lastText.Trim()) { $lastText } else { '(empty report)' }
            return @{ name = $Name; status = 'ok'; turns = ($turn + 1); report = $report }
        }
        foreach ($tc in @($msg.tool_calls)) {
            $result = Invoke-AgentSubTool $tc $false
            $history = @($history) + @{ role = 'tool'; content = (ConvertTo-Json -InputObject $result -Depth 8 -Compress); tool_call_id = $tc.id; name = $tc.name }
        }
    }
    $report = if ($lastText) { $lastText } else { 'hit the turn limit before finishing' }
    return @{ name = $Name; status = 'incomplete'; turns = $maxTurns; report = $report }
}

function Invoke-Subagents {
    param($Channel, [array]$Tasks)
    $clean = @()
    foreach ($t in $Tasks) { if ($t.task -and $t.task.Trim()) { $clean += @{ name = "$($t.name)"; task = "$($t.task)" } } }
    if ($clean.Count -gt 5) { $clean = @($clean | Select-Object -First 5) }
    if ($clean.Count -eq 0) {
        return @{ error = 'no tasks given — pass tasks=[{"name": ..., "task": ...}, ...]' }
    }
    $names = @()
    for ($i = 0; $i -lt $clean.Count; $i++) {
        if (-not $clean[$i].name) { $clean[$i].name = "agent-$($i + 1)" }
        $names += $clean[$i].name
    }
    Send-ChannelMessage $Channel ("🧩 Spawning {0} subagent(s): {1}" -f $clean.Count, ($names -join ', '))

    # each subagent runs in a child pwsh process (Start-ThreadJob can't see
    # script-scope functions) — dot-source this script as a library there
    $local = $false
    if ($Channel.rest -or -not (Get-Command Start-Job -ErrorAction SilentlyContinue)) { $local = $true }
    if ($local) {
        $results = @()
        foreach ($t in $clean) { $results += (Invoke-SubTask -Name $t.name -Task $t.task) }
        return @{ results = $results }
    }
    $jobs = @()
    foreach ($t in $clean) {
        $jobs += Start-Job -ScriptBlock {
            param($ScriptPath, $Name, $Task)
            $env:SYSTEMC_LIBRARY = '1'
            . $ScriptPath
            Invoke-SubTask -Name $Name -Task $Task | ConvertTo-Json -Depth 8 -Compress
        } -ArgumentList $script:SCRIPT_PATH, $t.name, $t.task
    }
    $jsons = @(($jobs | Wait-Job) | Receive-Job)
    foreach ($j in $jobs) { Remove-Job $j -Force }
    $results = @($jsons | ForEach-Object { try { $_ | ConvertFrom-Json } catch { @{ report = "$_" } } })
    return @{ results = $results }
}

function Invoke-AgentTool {
    param($Channel, $ToolCall, [string]$CurrentSession, [System.Collections.Generic.List[string]]$CommandsUsed)
    $name = $ToolCall.name
    $input = $ToolCall.input
    if ($null -eq $input) { $input = @{} }
    switch ($name) {
        'run_shell' {
            $command = "$($input.command)"
            $CommandsUsed.Add($command) | Out-Null
            if ((Test-Destructive $command) -and -not (Request-ChannelApproval $Channel $command $false 60)) {
                return @{ error = 'command was denied by user'; exit_code = -1 }
            }
            return Invoke-ShellTool $command
        }
        'run_shell_elevated' {
            $command = "$($input.command)"
            if (-not (Test-ElevatedEnabled)) { return @{ error = 'elevated commands are turned off on this machine'; exit_code = -1 } }
            if (-not (Request-ChannelApproval $Channel ("[ELEVATED / sudo]`n$command") $true 90)) {
                return @{ error = 'elevated command was denied by user'; exit_code = -1 }
            }
            return Invoke-ShellTool $command $true
        }
        'run_python' { return Invoke-PythonTool "$($input.code)" }
        'read_file' { return Read-FileTool "$($input.path)" }
        'write_file' { return Write-FileTool "$($input.path)" "$($input.content)" }
        'list_dir' { return List-DirTool "$($input.path)" }
        'web_search' {
            $max = 5
            if ($input.max_results) { try { $max = [int]$input.max_results } catch {} }
            return Invoke-WebSearchTool "$($input.query)" $max
        }
        'remember' { return Save-Fact "$($input.fact)" }
        'reset_session' {
            $ref = if ($input.session) { "$($input.session)" } else { 'current' }
            $resolved = Resolve-SessionRef $ref $CurrentSession
            if (-not $resolved.ok) {
                $known = @(Get-SessionList | Select-Object -First 10 | ForEach-Object { $_.key })
                return @{ error = $resolved.error; known_sessions = $known }
            }
            $key = $resolved.key
            if ($key -ne $CurrentSession) {
                $info = Get-SessionList | Where-Object { $_.key -eq $key } | Select-Object -First 1
                $what = if ($info -and $info.label) { "$key ($($info.label))" } else { $key }
                $turns = if ($info) { $info.turns } else { 0 }
                if (-not (Request-ChannelApproval $Channel "[reset_session] Clear the conversation history of $what ($turns turn(s)). Long-term memory is not affected." $false 60)) {
                    return @{ error = 'reset of another session was denied by the owner'; session = $key }
                }
            }
            $res = Reset-Session $key
            $res['long_term_memory'] = 'untouched'
            if ($key -eq $CurrentSession) {
                $res['note'] = "This conversation's history is cleared once your current reply is sent; the next message starts from a blank slate."
            }
            return $res
        }
        'send_message' {
            Send-ChannelMessage $Channel "$($input.content)"
            return @{ sent = $true }
        }
        'send_file' { return Send-ChannelFile $Channel "$($input.path)" "$($input.caption)" }
        'fetch_channel_messages' {
            $limit = 20
            if ($input.limit) { try { $limit = [int]$input.limit } catch {} }
            $msgs = @(Get-ChannelHistory $Channel $limit)
            return @{ messages = $msgs; count = $msgs.Count }
        }
        'spawn_subagent' { return Invoke-Subagents $Channel @(@{ name = "$($input.name)"; task = "$($input.task)" }) }
        'spawn_subagents' {
            $tasks = @()
            foreach ($t in @($input.tasks)) { if ($t) { $tasks += @{ name = "$($t.name)"; task = "$($t.task)" } } }
            return Invoke-Subagents $Channel $tasks
        }
        default { return @{ error = "unknown tool: $name" } }
    }
}

function Invoke-AgentTurn {
    param($Channel, [string]$UserText, [System.Collections.Generic.List[hashtable]]$History, $Author)
    $prompt = if ($Author) { "[from: $($Author.name) (@$($Author.name), id $($Author.id))]: $UserText" } else { $UserText }
    $History.Add(@{ role = 'user'; content = $prompt }) | Out-Null

    # re-inject memory fresh every turn (another channel may have written it)
    $ctx = Get-ProviderContext
    $systemPrompt = Get-SystemPrompt -Ctx $ctx
    $memContext = Get-MemoryContext
    if ($memContext) { $systemPrompt = "$systemPrompt`n`n$memContext" }
    if (-not (Test-ElevatedEnabled)) {
        $systemPrompt = $systemPrompt.Replace('If run_shell fails with "Permission denied", retry with run_shell_elevated', 'If run_shell fails with "Permission denied", you may not have elevated access here: tell the user what needs doing instead of trying to work around it')
    }

    $schemas = Get-AgentSchemas $Channel
    $currentSession = New-SessionKey (Get-ChannelPlatform $Channel) (Get-ChannelId $Channel)
    $commandsUsed = [System.Collections.Generic.List[string]]::new()
    $toolCounts = [ordered]@{}
    $finalText = ''
    $hitLimit = $true

    for ($turn = 0; $turn -lt (Get-MaxTurns); $turn++) {
        $assistant = Invoke-LlmStep -SystemPrompt $systemPrompt -History $History -Schemas $schemas
        $History.Add($assistant) | Out-Null

        if (-not ($assistant.tool_calls -and @($assistant.tool_calls).Count -gt 0)) {
            if ($assistant.content) {
                $finalText = $assistant.content
                $hitLimit = $false
                if ((Show-CommandsUsedFlag) -or (Show-ToolsUsedFlag)) {
                    $lines = @()
                    if ((Show-CommandsUsedFlag) -and $commandsUsed.Count) {
                        $lines += '-# __Commands executed__'
                        foreach ($c in ($commandsUsed | Select-Object -Unique | Select-Object -First 50)) { $lines += "-# - $c" }
                    }
                    if ((Show-ToolsUsedFlag) -and $toolCounts.Count) {
                        $lines += '-# __Tools Used__'
                        foreach ($k in $toolCounts.Keys) { $lines += "-# $k ($($toolCounts[$k]))" }
                    }
                    if ($lines.Count) { Send-ChannelMessage $Channel ($lines -join "`n") }
                }
                Send-ChannelMessage $Channel $assistant.content
            }
            break
        }

        foreach ($tc in @($assistant.tool_calls)) {
            if ($toolCounts.Contains($tc.name)) { $toolCounts[$tc.name]++ } else { $toolCounts[$tc.name] = 1 }
            $result = Invoke-AgentTool -Channel $Channel -ToolCall $tc -CurrentSession $currentSession -CommandsUsed $commandsUsed
            $History.Add(@{ role = 'tool'; content = (ConvertTo-Json -InputObject $result -Depth 8 -Compress); tool_call_id = $tc.id; name = $tc.name }) | Out-Null
        }
    }
    if ($hitLimit) { Send-ChannelMessage $Channel '_(hit the max tool-call limit for this turn — stopping)_' }
    Write-MemoryTurn (Get-ChannelLabel $Channel) $UserText $finalText
}

# ── Discord gateway (System.Net.WebSockets.ClientWebSocket) ────────────────
$script:HeartbeatMs = 41250
$script:DispatchJobs = @()

function Send-WsText {
    param($Ws, [string]$Payload)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Payload)
    [void]$Ws.SendAsync([ArraySegment[byte]]::new($bytes), [System.Net.WebSockets.WebSocketMessageType]::Text, $true, [System.Threading.CancellationToken]::None).Result
}

function Receive-WsText {
    # one full message; sends heartbeats while waiting so the gateway stays alive
    param($Ws, [System.Threading.CancellationToken]$Ct, [ref]$LastSeq)
    $buffer = New-Object 'byte[]' 524288
    $parts = New-Object System.Text.StringBuilder
    $nextBeat = (Get-Date).AddMilliseconds($script:HeartbeatMs)
    while ($true) {
        if ((Get-Date) -ge $nextBeat -and $Ws.State -eq 'Open') {
            $d = if ($LastSeq.Value -ge 0) { $LastSeq.Value } else { $null }
            Send-WsText -Ws $Ws -Payload (@{ op = 1; d = $d } | ConvertTo-Json -Compress)
            $nextBeat = (Get-Date).AddMilliseconds($script:HeartbeatMs)
        }
        $seg = [ArraySegment[byte]]::new($buffer)
        $recv = $Ws.ReceiveAsync($seg, $Ct)
        while (-not $recv.IsCompleted) {
            Start-Sleep -Milliseconds 50
            if ((Get-Date) -ge $nextBeat -and $Ws.State -eq 'Open') {
                Send-WsText -Ws $Ws -Payload (@{ op = 1; d = $(if ($LastSeq.Value -ge 0) { $LastSeq.Value } else { $null }) } | ConvertTo-Json -Compress)
                $nextBeat = (Get-Date).AddMilliseconds($script:HeartbeatMs)
            }
        }
        $result = $recv.Result
        if ($result.MessageType -eq [System.Net.WebSockets.WebSocketMessageType]::Close) { throw 'gateway closed' }
        if ($result.Count -gt 0) {
            [void]$parts.Append([System.Text.Encoding]::UTF8.GetString($buffer, 0, $result.Count))
        }
        if ($result.EndOfMessage) {
            return $parts.ToString()
        }
    }
}

function Start-GatewaySession {
    param([string]$Token, [string]$Mode, [string]$Wake, [string[]]$Allowed)
    $ws = [System.Net.WebSockets.ClientWebSocket]::new()
    $ct = [System.Threading.CancellationToken]::None
    $ws.ConnectAsync([Uri]$script:GATEWAY_URL, $ct).Wait()
    if ($ws.State -ne [System.Net.WebSockets.WebSocketState]::Open) { throw 'connect: websocket failed' }

    $identified = $false
    $lastSeq = [ref]([long]-1)
    $meId = ''
    $script:HeartbeatMs = 41250

    while ($true) {
        $raw = Receive-WsText -Ws $ws -Ct $ct -LastSeq $lastSeq
        if (-not $raw) { break }
        try { $v = $raw | ConvertFrom-Json } catch { continue }
        $op = $v.op
        if ($op -eq 10) {  # HELLO
            if ($v.d.heartbeat_interval) { $script:HeartbeatMs = [int]$v.d.heartbeat_interval }
            if ($identified) {
                Send-WsText -Ws $ws -Payload (@{ op = 6; d = @{ token = $Token; seq = $lastSeq.Value } } | ConvertTo-Json -Compress)
            } else {
                Send-WsText -Ws $ws -Payload (@{
                    op = 2; d = @{ token = $Token; intents = $script:INTENTS
                                   properties = @{ os = 'linux'; browser = 'systemc-pwsh'; device = 'systemc-pwsh' } }
                } | ConvertTo-Json -Compress -Depth 6)
                $identified = $true
            }
        } elseif ($op -eq 0) {  # dispatch
            if ($null -ne $v.s) { $lastSeq.Value = [long]$v.s }
            if ($v.t -eq 'READY') {
                $meId = $v.d.user.id
                Write-Host "✓ logged in as $($v.d.user.username) (id $meId)" -ForegroundColor Green
            } elseif ($v.t -eq 'MESSAGE_CREATE') {
                # one child pwsh job per message so heartbeats keep flowing during turns
                $script:DispatchJobs = @($script:DispatchJobs | Where-Object { $_.State -eq 'Running' })
                $script:DispatchJobs += Start-Job -ScriptBlock {
                    param($ScriptPath, $D, $MeId, $Mode, $Wake, $Allowed)
                    $env:SYSTEMC_LIBRARY = '1'
                    . $ScriptPath
                    Invoke-MessageDispatch $D $MeId $Mode $Wake $Allowed
                } -ArgumentList $script:SCRIPT_PATH, $v.d, $meId, $Mode, $Wake, $Allowed
            }
        } elseif ($op -eq 7 -or $op -eq 9) {
            $ws.Dispose()
            throw 'server asked for reconnect'
        }
        # op 11 heartbeat ACK and friends are ignored
    }
    $ws.Dispose()
}

function Invoke-MessageDispatch {
    param($d, [string]$MeId, [string]$Mode, [string]$Wake, [string[]]$Allowed)
    $token = Get-Cfg 'DISCORD_BOT_TOKEN'
    $content = "$($d.content)"
    if (-not $content) { return }
    $authorBot = [bool]($d.author.bot)
    if ($d.author.id -eq $MeId -or $authorBot) { return }
    $channelId = "$($d.channel_id)"
    if ($Allowed.Count -gt 0 -and $Allowed -notcontains $channelId) { return }

    $authorName = if ($d.author.global_name) { $d.author.global_name } else { $d.author.username }
    $author = @{ id = "$($d.author.id)"; name = $authorName }
    $rest = @{ token = $token; channel_id = $channelId; app_id = $MeId }
    $rest['label'] = $(if ($d.guild_id) { "#$($d.channel_name)" } else { "DM with $authorName" })
    $channel = New-Channel -Discord $rest

    # !command shortcut: bypass the LLM, run shell directly
    if ($content.Length -gt 1 -and $content[0] -eq '!') {
        $command = $content.Substring(1).Trim()
        try { Send-DiscordApi $rest 'Post' "/channels/$channelId/typing" | Out-Null } catch {}
        if ((Test-Destructive $command) -and -not (Request-ChannelApproval $channel $command $false 60)) { return }
        $result = Invoke-ShellTool $command
        $stdout = "$($result.stdout)".Trim()
        $out = if ($stdout) { $stdout.Substring([Math]::Max(0, $stdout.Length - 1900)) } else { '(no output)' }
        $exit = $result.exit_code
        Send-ChannelMessage $channel (('```' + "`n" + $out + "`n" + '```' + "`nexit: `"'" + "`"'" + "`n") )
        return
    }

    # /reset: clear this channel's conversation context
    $stripped = $content.Replace("<@$MeId>", '').Replace("<@!$MeId>", '')
    if ($stripped.Trim() -match '^/reset$') {
        $res = Reset-Session (New-SessionKey 'discord' $channelId)
        if (-not $res.error) {
            Send-ChannelMessage $channel '🧹 Context cleared for this channel. Long-term memory is untouched.'
        } else {
            Send-ChannelMessage $channel "⚠️ couldn't clear the session."
        }
        return
    }

    # should the agent respond?
    $lower = $content.ToLower()
    $mentioned = $lower.Contains("<@$MeId>") -or $lower.Contains("<@!$MeId>")
    $responds = switch ($Mode) {
        'everything' { $true }
        'mention' { $mentioned }
        default { $mentioned -or $lower.StartsWith($Wake) }
    }
    if (-not $responds) { return }

    # strip the wake word (mention already stripped in the /reset pass)
    $prompt = $stripped.Trim()
    if ($prompt.ToLower().StartsWith($Wake)) {
        $prompt = $prompt.Substring($Wake.Length).Trim()
    }
    if (-not $prompt) { $prompt = $content }

    try { Send-DiscordApi $rest 'Post' "/channels/$channelId/typing" | Out-Null } catch {}

    # attachments auto-download into the workspace
    $saved = @()
    if ($d.attachments) {
        foreach ($att in @($d.attachments)) {
            if (-not $att -or -not $att.url -or -not $att.filename) { continue }
            try {
                $safe = $att.filename.Replace('/', '_').Replace('..', '_')
                Invoke-WebRequest -Uri "$($att.url)" -OutFile (Join-Path (Get-WorkDir) $safe) -TimeoutSec 60
                $saved += $safe
            } catch { Write-Host "[discord] attachment download failed: $_" -ForegroundColor DarkRed }
        }
    }
    if ($saved.Count -gt 0) {
        $prompt += "`n`n[Attached files saved to workspace: $($saved -join ', ')]"
    }

    $key = New-SessionKey 'discord' $channelId
    $hist = [System.Collections.Generic.List[hashtable]]::new()
    foreach ($m in (Get-SessionHistory $key)) {
        $msg = @{ role = "$($m.role)"; content = "$($m.content)" }
        if ($m.tool_call_id) { $msg['tool_call_id'] = "$($m.tool_call_id)" }
        if ($m.name) { $msg['name'] = "$($m.name)" }
        if ($m.tool_calls) {
            $tcs = @()
            foreach ($tc in @($m.tool_calls)) { $tcs += @{ id = "$($tc.id)"; name = "$($tc.name)"; input = $tc.input } }
            $msg['tool_calls'] = $tcs
        }
        $hist.Add($msg) | Out-Null
    }
    Invoke-AgentTurn -Channel $channel -UserText $prompt -History $hist -Author $author

    # trim to the last 60 messages, snapped forward to a user boundary
    $final = @($hist)
    if ($final.Count -gt 60) {
        $start = $final.Count - 60
        while ($start -lt $final.Count -and $final[$start].role -ne 'user') { $start++ }
        $final = @($final[$start..($final.Count - 1)])
    }
    Save-SessionHistory $key $final
    Set-SessionLabel $key $rest.label
}

# ── control panel (System.Net.HttpListener + embedded page) ────────────────
function Send-PanelJson {
    param($Res, $Data, [int]$Status = 200)
    $Res.StatusCode = $Status
    $Res.ContentType = 'application/json'
    $bytes = [System.Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -InputObject $Data -Depth 10 -Compress))
    $Res.OutputStream.Write($bytes, 0, $bytes.Length)
}

function Get-PanelEnv {
    $ctx = Get-ProviderContext
    return @{
        release = $script:SYSTEMC_DISPLAY
        model = $ctx.model
        provider = (Get-Cfg 'AGENT_PROVIDER' 'ollama')
        workdir = (Get-WorkDir)
        version = $script:SYSTEMC_DISPLAY
    }
}

function Start-Panel {
    param([int]$Port = 7842)
    $uiPath = Join-Path (Split-Path $script:SCRIPT_PATH -Parent) 'panel_ui.html'
    if (-not (Test-Path $uiPath)) { $uiPath = Join-Path $PWD 'panel_ui.html' }
    $ui = @(Get-Content $uiPath -Raw -ErrorAction SilentlyContinue)[0]
    if (-not $ui) { $ui = '<h1>panel UI missing</h1>' }

    $listener = [System.Net.HttpListener]::new()
    $listener.Prefixes.Add("http://localhost:$Port/")
    try { $listener.Start() } catch {
        Write-Host "[panel] couldn't bind port ${Port}: $_" -ForegroundColor DarkRed
        exit 1
    }
    Write-Host '╔════════════════════════════════════╗'
    Write-Host '║   systemc control panel (pwsh)     ║'
    Write-Host '╚════════════════════════════════════╝'
    Write-Host "  open → http://localhost:$Port"
    Write-Host "  release → $script:SYSTEMC_DISPLAY"
    Write-Host ''

    while ($listener.IsListening) {
        $ctx = $listener.GetContext()
        $req = $ctx.Request
        $res = $ctx.Response
        $body = ''
        if ($req.HasEntityBody) {
            $reader = New-Object System.IO.StreamReader($req.InputStream, [System.Text.Encoding]::UTF8)
            $body = $reader.ReadToEnd()
        }
        $payload = try { $body | ConvertFrom-Json -ErrorAction Stop } catch { @{} }
        $route = "{0} {1}" -f $req.HttpMethod, $req.Url.AbsolutePath
        try {
            if ($route -eq 'GET /' -or $route -eq 'GET /panel') {
                $res.StatusCode = 200
                $res.ContentType = 'text/html; charset=utf-8'
                $bytes = [System.Text.Encoding]::UTF8.GetBytes($ui)
                $res.OutputStream.Write($bytes, 0, $bytes.Length)
            } elseif ($route -eq 'GET /config') {
                Send-PanelJson $res (Get-PanelEnv)
            } elseif ($route -eq 'GET /api/status') {
                $out = Get-PanelEnv
                $out['discord'] = [bool](Get-Cfg 'DISCORD_BOT_TOKEN')
                $out['history_dir'] = Get-HistoryDir
                $out['memory_dir'] = (Get-MemoryContext).Length
                Send-PanelJson $res $out
            } elseif ($route -eq 'GET /sessions') {
                Send-PanelJson $res @(Get-SessionList)
            } elseif ($route -eq 'POST /sessions/reset') {
                $r = Reset-Session "$($payload.key)"
                Send-PanelJson $res $r $(if ($r.error) { 400 } else { 200 })
            } elseif ($route -eq 'GET /memory') {
                Send-PanelJson $res @{
                    long_term = Read-Capped (Join-Path (Get-MemoryDir) 'MEMORY.md') 100000
                    today = Read-Capped (Join-Path (Get-MemoryDir) "daily/$(Get-DayStamp).md") 100000
                    yesterday = Read-Capped (Join-Path (Get-MemoryDir) "daily/$(Get-DayStamp -1).md") 100000
                    date = (Get-DayStamp)
                }
            } elseif ($route -eq 'POST /memory/remember') {
                Send-PanelJson $res (Save-Fact "$($payload.fact)")
            } elseif ($route -eq 'POST /tools/run_shell') {
                Send-PanelJson $res (Invoke-ShellTool "$($payload.command)")
            } elseif ($route -eq 'POST /tools/run_shell_elevated') {
                if (-not (Test-ElevatedEnabled)) {
                    Send-PanelJson $res @{ error = 'elevated commands are turned off on this machine'; exit_code = -1 } 403
                } else {
                    Send-PanelJson $res (Invoke-ShellTool "$($payload.command)" $true)
                }
            } elseif ($route -eq 'POST /tools/read_file') {
                Send-PanelJson $res (Read-FileTool "$($payload.path)")
            } elseif ($route -eq 'POST /tools/write_file') {
                Send-PanelJson $res (Write-FileTool "$($payload.path)" "$($payload.content)")
            } elseif ($route -eq 'POST /tools/list_dir') {
                Send-PanelJson $res (List-DirTool $(if ($payload.path) { "$($payload.path)" } else { '.' }))
            } elseif ($route -eq 'POST /tools/run_python') {
                Send-PanelJson $res (Invoke-PythonTool "$($payload.code)")
            } elseif ($route -eq 'POST /tools/web_search') {
                $max = 5
                if ($payload.max_results) { try { $max = [int]$payload.max_results } catch {} }
                Send-PanelJson $res (Invoke-WebSearchTool "$($payload.query)" $max)
            } elseif ($route -eq 'POST /ollama/chat') {
                # streaming NDJSON proxy so the browser UI can chat (tools stay local)
                $cfg = Get-OllamaSettings
                $headers = @{ 'Content-Type' = 'application/json' }
                if ($cfg.api_keys.Count -gt 0) { $headers['Authorization'] = "Bearer $($cfg.api_keys[0])" }
                try {
                    $r = Invoke-WebRequest -Uri "$($cfg.host)/api/chat" -Method Post -Headers $headers -Body $body -TimeoutSec 600 -SkipHttpErrorCheck
                    $res.StatusCode = 200
                    $res.ContentType = 'application/x-ndjson'
                    $bytes = [System.Text.Encoding]::UTF8.GetBytes($r.Content)
                    $res.OutputStream.Write($bytes, 0, $bytes.Length)
                } catch {
                    Send-PanelJson $res @{ error = "$_" } 502
                }
            } elseif ($route -eq 'GET /service/status') {
                $active = (& systemctl --user is-active systemc.service 2>$null | Out-String).Trim()
                Send-PanelJson $res @{ active = $active; enabled_by_panel = $true }
            } elseif ($route -eq 'POST /service/install') {
                $r = Invoke-ServiceInstall @{ profile = $null; linger = [bool]$payload.linger; system = $false }
                Send-PanelJson $res $r
            } elseif ($route -eq 'POST /service/uninstall') {
                Send-PanelJson $res (Invoke-ServiceUninstall $null $false)
            } else {
                Send-PanelJson $res @{ error = 'not found'; path = $req.Url.AbsolutePath } 404
            }
        } catch {
            Send-PanelJson $res @{ error = "$_" } 500
        }
        $res.OutputStream.Close()
    }
}

# ── systemd (service.rs port) ───────────────────────────────────────────────
function Get-UserUnitDir {
    $xdg = $env:XDG_CONFIG_HOME
    if (-not $xdg) { $xdg = Join-Path $HOME '.config' }
    return (Join-Path $xdg 'systemd/user')
}

function Get-UnitName {
    param([string]$Profile)
    if ($Profile -and $Profile -ne 'default') { return "systemc-$Profile.service" }
    return 'systemc.service'
}

function Invoke-ServiceInstall {
    param([hashtable]$Opts)
    $profile = $Opts.profile
    $name = Get-UnitName $profile
    if ($Opts.system -and (whoami) -ne 'root') {
        return @{ ok = $false; error = '--system needs root (run with sudo)' }
    }
    $dir = if ($Opts.system) { '/etc/systemd/system' } else { (Get-UserUnitDir) }
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    $home = if ($HOME) { $HOME } else { '/root' }
    $exec = $script:SCRIPT_PATH
    $profileArg = if ($profile -and $profile -ne 'default') { " --profile $profile" } else { '' }
    $wanted = if ($Opts.system) { 'multi-user.target' } else { 'default.target' }
    $nl = "`n"
    $unit = "# Generated by systemc (pwsh) — edit freely, re-run './systemc.ps1 service install' to regenerate$nl" +
        "[Unit]$nl" +
        "Description=SystemC agent ($script:SYSTEMC_DISPLAY) — the PowerShell rewrite$nl" +
        "After=network-online.target$nl" +
        "Wants=network-online.target$nl" +
        "$nl" +
        "[Service]$nl" +
        "ExecStart=/usr/bin/pwsh -File $exec run$profileArg$nl" +
        "WorkingDirectory=$home$nl" +
        "Restart=on-failure$nl" +
        "RestartSec=5$nl" +
        "Environment=SYSTEMC_HOME=$home$nl" +
        "$nl" +
        "[Install]$nl" +
        "WantedBy=$wanted$nl"
    $path = Join-Path $dir $name
    [System.IO.File]::WriteAllText($path, $unit)
    Write-Host "✓ wrote $path"

    $sctl = if ($Opts.system) { @() } else { @('--user') }
    & systemctl @sctl daemon-reload
    if ($LASTEXITCODE -ne 0) { return @{ ok = $false; error = 'daemon-reload failed' } }
    & systemctl @sctl enable --now $name
    if ($LASTEXITCODE -ne 0) { return @{ ok = $false; error = 'enable failed' } }
    if ($Opts.linger -and -not $Opts.system) {
        & loginctl enable-linger (whoami) 2>$null
        if ($LASTEXITCODE -ne 0) { Write-Host 'warning: enable-linger failed, service only starts at login' -ForegroundColor Yellow }
    }
    Write-Host ''
    Write-Host "✓ $name installed and started."
    Write-Host "  status:  systemctl $(if (-not $Opts.system) { '--user ' })status $name"
    Write-Host "  logs:    journalctl $(if (-not $Opts.system) { '--user ' })-f -u $name"
    return @{ ok = $true; unit = $name; path = $path }
}

function Invoke-ServiceUninstall {
    param([string]$Profile, [bool]$System)
    $name = Get-UnitName $Profile
    $sctl = if ($System) { @() } else { @('--user') }
    & systemctl @sctl disable --now $name 2>$null
    $dir = if ($System) { '/etc/systemd/system' } else { (Get-UserUnitDir) }
    Remove-Item (Join-Path $dir $name) -Force -ErrorAction SilentlyContinue
    & systemctl @sctl daemon-reload 2>$null
    if (-not $System) {
        & systemctl @sctl reset-failed 2>$null
    }
    Write-Host "✓ $name uninstalled."
    return @{ ok = $true; unit = $name }
}

function Get-ServiceStatus {
    $profile = Get-Cfg 'SYSTEMC_PROFILE_ID'
    $name = Get-UnitName $profile
    $out = (& systemctl --user status $name 2>&1 | Out-String).Trim()
    if (-not $out -or $out -match 'inactive|could not be found|not found|Failed') {
        return "(inactive or not installed — run './systemc.ps1 service install')`n$out"
    }
    return $out
}

# ── main dispatch ───────────────────────────────────────────────────────────
function Get-ServiceArgs {
    param([string[]]$Args_)
    $o = @{ profile = $null; linger = $false; system = $false }
    for ($i = 0; $i -lt $Args_.Count; $i++) {
        if ($Args_[$i] -in @('--profile', '-p') -and $i + 1 -lt $Args_.Count) {
            $o.profile = $Args_[$i + 1]; $i++
        } elseif ($Args_[$i] -eq '--linger') { $o.linger = $true }
        elseif ($Args_[$i] -eq '--system') { $o.system = $true }
    }
    return $o
}

function Show-Help {
    Show-Banner
    ''
    'subcommands:'
    '  run                      start the Discord agent (needs DISCORD_BOT_TOKEN in .env)'
    '  panel [--port N]         start the web control panel app (default port 7842)'
    '  chat                     terminal agent: talk to the agent in this shell'
    '  models                   list model IDs the configured Ollama host offers'
    '  service install|uninstall|status [--profile <id>] [--linger] [--system]'
}

function Start-Main {
    Import-DotEnv
    if ($Command -in @('--install-service', '--uninstall-service')) {
        Write-Host (Show-Banner)
        $rest2 = @($Rest | Select-Object -Skip 1)  # skip the action word used as first arg in old py
        $opts = Get-ServiceArgs $rest2
        if ($Command -eq '--install-service') {
            Invoke-ServiceInstall $opts | Out-Null
        } else {
            Invoke-ServiceUninstall $opts.profile $opts.system | Out-Null
        }
        return 0
    }
    $extraArgs = @(if ($Rest.Count -gt 1) { @($Rest | Select-Object -Skip 1) } else { @() })
    switch ($Command) {
        'run' {
            if (-not (Get-Cfg 'DISCORD_BOT_TOKEN')) {
                Write-Host 'No DISCORD_BOT_TOKEN in .env — create a bot token (https://discord.com/developers/applications),' -ForegroundColor Red
                Write-Host 'put it in .env/$SYSTEMC_HOME, and enable the Message Content intent for the bot.' -ForegroundColor Red
                return 1
            }
            Write-Host (Show-Banner)
            try { Start-DiscordBot -Token (Get-Cfg 'DISCORD_BOT_TOKEN') } catch {
                Write-Host "[agent] $_" -ForegroundColor Red
                return 1
            }
        }
        { $_ -in @('panel', 'webui') } {
            $port = 7842
            for ($i = 0; $i -lt $Rest.Count; $i++) {
                if ($Rest[$i] -eq '--port' -and $i + 1 -lt $Rest.Count) {
                    try { $port = [int]$Rest[$i + 1] } catch {}
                }
            }
            Start-Panel -Port $port
        }
        'chat' {
            Write-Host (Show-Banner)
            Write-Host '(terminal agent — type a message, ctrl-d to exit)' -ForegroundColor DarkGray
            $history = [System.Collections.Generic.List[hashtable]]::new()
            $channel = New-Channel
            while ($true) {
                Write-Host 'you ▸ ' -NoNewline -ForegroundColor Cyan
                $line = [console]::In.ReadLine()
                if ($null -eq $line) { break }
                $text = $line.Trim()
                if (-not $text) { continue }
                try {
                    Invoke-AgentTurn -Channel $channel -UserText $text -History $history -Author $null
                } catch {
                    Write-Host "[agent] $_" -ForegroundColor Red
                }
            }
        }
        'models' {
            Write-Host (Show-Banner)
            $cfg = Get-OllamaSettings
            Write-Host "[$($cfg.mode)] $($cfg.host)"
            $keys = @($cfg.api_keys) + @('')
            foreach ($key in $keys) {
                try {
                    foreach ($n in (Get-OllamaModels -OllamaHost $cfg.host -Key $key)) { Write-Host "  $n" }
                    break
                } catch {
                    Write-Host "  key failed: $_" -ForegroundColor DarkGray
                }
            }
        }
        'service' {
            Write-Host (Show-Banner)
            $action = if ($Rest.Count -gt 0) { $Rest[0] } else { 'status' }
            switch ($action) {
                'install' { Invoke-ServiceInstall (Get-ServiceArgs $extraArgs) | Out-Null }
                'uninstall' {
                    $o = Get-ServiceArgs $extraArgs
                    Invoke-ServiceUninstall -Profile $o.profile -System $o.system | Out-Null
                }
                'status' { Write-Host (Get-ServiceStatus) }
                default { Write-Host "unknown service action: $action" -ForegroundColor Red }
            }
        }
        { $_ -in @('version', '--version', '-V') } { Write-Host (Show-Banner) }
        '--tui' {
            Write-Host "--tui removed: the pwsh control panel is './systemc.ps1 panel'" -ForegroundColor Red
            return 1
        }
        { $_ -in @('help', '--help', '-h') } { Show-Help }
        default {
            Write-Host "unknown subcommand: $Command" -ForegroundColor Red
            Show-Help
            return 1
        }
    }
    return 0
}

# dot-sourced as a function library (subagent / dispatch jobs) → don't exec
if ($env:SYSTEMC_LIBRARY) {
    # child jobs start in an arbitrary cwd: pin the app dir to the repo root
    if (-not $env:SYSTEMC_HOME) {
        $env:SYSTEMC_HOME = (Split-Path (Split-Path $PSCommandPath -Parent) -Parent)
    }
    [System.Environment]::CurrentDirectory = $env:SYSTEMC_HOME
    Import-DotEnv   # child jobs don't run Start-Main: load .env here too
} else {
    exit (Start-Main)
}
