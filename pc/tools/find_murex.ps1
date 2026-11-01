<#
  開いているウィンドウのうち、タイトル・クラス名・プロセス名・実行ファイルのパス・コマンドラインの
  どれかに "MURex" (大文字小文字は区別しない) を含むものを調べて出力する。読み取り専用 (何も変更しない)。
  結果はデスクトップの murex_windows.txt にも保存する。

  使い方 (PowerShell):
    powershell -ExecutionPolicy Bypass -File .\pc\tools\find_murex.ps1
    powershell -ExecutionPolicy Bypass -File .\pc\tools\find_murex.ps1 -Pattern RexMU
    # ミューテックスも一覧にする (Sysinternals の handle64.exe が必要。管理者の PowerShell で実行)
    powershell -ExecutionPolicy Bypass -File .\pc\tools\find_murex.ps1 -HandleExe C:\Tools\handle64.exe
#>
param(
    [string]$Pattern = 'MURex',
    [string]$HandleExe = '',
    [int]$MaxChildren = 30
)

$ErrorActionPreference = 'Stop'

if (-not ('AkmWin' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public static class AkmWin {
    public delegate bool EnumProc(IntPtr h, IntPtr l);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }

    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc cb, IntPtr l);
    [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr p, EnumProc cb, IntPtr l);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetClassName(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr h, uint cmd);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr h, out RECT r);

    public static List<IntPtr> TopLevel() {
        var l = new List<IntPtr>();
        EnumWindows((h, x) => { l.Add(h); return true; }, IntPtr.Zero);
        return l;
    }
    public static List<IntPtr> Children(IntPtr p) {
        var l = new List<IntPtr>();
        EnumChildWindows(p, (h, x) => { l.Add(h); return true; }, IntPtr.Zero);
        return l;
    }
    public static string Text(IntPtr h) { var s = new StringBuilder(512); GetWindowText(h, s, s.Capacity); return s.ToString(); }
    public static string Cls(IntPtr h) { var s = new StringBuilder(256); GetClassName(h, s, s.Capacity); return s.ToString(); }
}
'@
}

$out = Join-Path ([Environment]::GetFolderPath('Desktop')) 'murex_windows.txt'
$log = [System.Collections.Generic.List[string]]::new()
function Log([string]$s) { $log.Add($s); Write-Host $s }
function Hex([IntPtr]$h) { '0x{0:X8}' -f $h.ToInt64() }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
$rx = [regex]::Escape($Pattern)
$fg = [AkmWin]::GetForegroundWindow()

$procs = @{}
Get-CimInstance Win32_Process | ForEach-Object { $procs[[int]$_.ProcessId] = $_ }

function MatchFields($title, $cls, $p) {
    $m = @()
    if ($title -match $rx) { $m += 'タイトル' }
    if ($cls -match $rx) { $m += 'クラス名' }
    if ($p) {
        if ($p.Name -match $rx) { $m += 'プロセス名' }
        if ($p.ExecutablePath -match $rx) { $m += '実行ファイル' }
        if ($p.CommandLine -match $rx) { $m += 'コマンドライン' }
    }
    $m
}

Log "=== '$Pattern' を含むウィンドウ ($(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))  管理者: $isAdmin ==="

$hits = 0
$hitPids = [System.Collections.Generic.HashSet[int]]::new()
foreach ($h in [AkmWin]::TopLevel()) {
    [uint32]$wpid = 0
    [void][AkmWin]::GetWindowThreadProcessId($h, [ref]$wpid)
    $p = $procs[[int]$wpid]
    $title = [AkmWin]::Text($h)
    $cls = [AkmWin]::Cls($h)
    $why = MatchFields $title $cls $p
    if (-not $why) { continue }

    $hits++
    [void]$hitPids.Add([int]$wpid)
    $wr = New-Object AkmWin+RECT
    $cr = New-Object AkmWin+RECT
    [void][AkmWin]::GetWindowRect($h, [ref]$wr)
    [void][AkmWin]::GetClientRect($h, [ref]$cr)
    $owner = [AkmWin]::GetWindow($h, 4)  # GW_OWNER
    $parent = if ($p) { $procs[[int]$p.ParentProcessId] } else { $null }

    Log ''
    Log ("--- [{0}] hwnd={1}{2}" -f $hits, (Hex $h), $(if ($h -eq $fg) { '  ← 今前面' } else { '' }))
    Log "タイトル       : $title"
    Log "クラス名       : $cls"
    Log "一致した所     : $($why -join ', ')"
    Log "PID / プロセス : $wpid  $(if ($p) { $p.Name } else { '(取得できず)' })"
    Log "実行ファイル   : $(if ($p -and $p.ExecutablePath) { $p.ExecutablePath } else { '(取得できず。管理者で実行すると見える場合あり)' })"
    Log "コマンドライン : $(if ($p -and $p.CommandLine) { $p.CommandLine } else { '(取得できず)' })"
    Log "親プロセス     : $(if ($parent) { "$($parent.ProcessId)  $($parent.Name)" } else { '(なし / 終了済み)' })"
    Log "起動時刻       : $(if ($p) { $p.CreationDate } else { '-' })"
    Log ("表示 / 最小化  : {0} / {1}   オーナー: {2}" -f [AkmWin]::IsWindowVisible($h), [AkmWin]::IsIconic($h),
        $(if ($owner -ne [IntPtr]::Zero) { Hex $owner } else { 'なし' }))
    Log ("ウィンドウ枠   : ({0},{1}) {2}x{3}   クライアント: {4}x{5}" -f $wr.L, $wr.T, ($wr.R - $wr.L), ($wr.B - $wr.T),
        ($cr.R - $cr.L), ($cr.B - $cr.T))

    $kids = [AkmWin]::Children($h)
    Log "子ウィンドウ   : $($kids.Count) 個"
    foreach ($c in ($kids | Select-Object -First $MaxChildren)) {
        Log ("    {0}  [{1}] `"{2}`"" -f (Hex $c), [AkmWin]::Cls($c), [AkmWin]::Text($c))
    }
}
if ($hits -eq 0) { Log ''; Log "(ウィンドウは見つかりませんでした。ゲームを起動してから実行するか、-Pattern を変えてください)" }

# ウィンドウを持たない (または見えない) 関連プロセス: ランチャーや常駐プロセスなど
Log ''
Log "=== ウィンドウ以外で '$Pattern' を含むプロセス ==="
$others = $procs.Values | Where-Object {
    -not $hitPids.Contains([int]$_.ProcessId) -and [int]$_.ProcessId -ne $PID -and
    ($_.Name -match $rx -or $_.ExecutablePath -match $rx -or $_.CommandLine -match $rx)
}
if ($others) {
    foreach ($p in $others) { Log ("{0,6}  {1}  {2}" -f $p.ProcessId, $p.Name, $p.CommandLine) }
} else { Log '(なし)' }

# ミューテックス (任意)。handle64.exe は管理者で動かさないとほかのプロセスのハンドルが見えない
if ($HandleExe) {
    Log ''
    Log '=== ミューテックス (handle64.exe の Mutant) ==='
    if (-not (Test-Path $HandleExe)) {
        Log "handle が見つかりません: $HandleExe"
    } elseif (-not $isAdmin) {
        Log '管理者の PowerShell で実行してください (今は一般ユーザーなので一覧が取れません)'
    } else {
        foreach ($id in $hitPids) {
            Log "-- PID $id"
            $lines = & $HandleExe -accepteula -nobanner -a -p $id 2>&1 | Select-String 'Mutant'
            if ($lines) { $lines | ForEach-Object { Log ("    " + $_.Line.Trim()) } } else { Log '    (名前付きのミューテックスなし)' }
        }
    }
}

$log | Out-File -FilePath $out -Encoding utf8
Write-Host "`n保存先: $out" -ForegroundColor Green
