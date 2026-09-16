# ============================================================
# 统一入口：一条命令起停本机全部相关环境
#
# 用法：
#   .\stack.ps1 up       # 启动全部（本项目 + 可观测平台）
#   .\stack.ps1 ps       # 查看全部状态
#   .\stack.ps1 stop     # 停止全部（保留容器）
#   .\stack.ps1 start    # 启动已存在的全部容器
#   .\stack.ps1 down     # 停止并删除容器（卷保留，数据不丢）
#   .\stack.ps1 logs backend
#
# ⚠ 为什么用脚本逐个调用，而不是 compose 的 `include:` 把文件合并：
#   实测 `include:` 会把被包含文件的服务**并入父项目**，后果是：
#     · 顶层 name 变成父目录名，本项目自己的 name: retail-assistant 被忽略
#     · 生成一整套**重复容器**，与现有容器抢 8000 端口
#     · langfuse 的 postgres 卷名从 langfuse_postgres_data 变成
#       <父项目>_postgres_data → **等于换库，langfuse 历史 trace 全部丢失**，
#       且新容器会与现有 langfuse 抢 3000 端口
#   而用 `-f <各自的文件>` 逐个调用时，每个栈都保持自己的项目名与卷名
#   （已验证：langfuse → name: langfuse，卷 langfuse_postgres_data）。
#
# 只想操作本项目时，照旧直接用 docker compose 即可（等价于以前的行为）：
#   docker compose up -d
# ============================================================

param(
    [Parameter(Position = 0)]
    [ValidateSet('up', 'down', 'stop', 'start', 'ps', 'logs')]
    [string]$Action = 'up',

    # logs 时的服务名（可选，不传则输出全部）
    [Parameter(Position = 1)]
    [string]$Service = ''
)

$ErrorActionPreference = 'Stop'

# 各栈的 compose 文件。加新栈时在这里追加即可。
# 注意：路径写各自的 compose 文件，**不要**试图用 include 合并。
$Stacks = @(
    @{ Name = '本项目 (retail-assistant)'; File = (Join-Path $PSScriptRoot 'docker-compose.yml') }
    @{ Name = '可观测平台 (langfuse)';     File = 'D:\langfuse\docker-compose.yml' }
)

function Invoke-Stack {
    param([string]$Name, [string]$File, [string]$Act, [string[]]$Extra)

    if (-not (Test-Path $File)) {
        Write-Host "`n[跳过] $Name —— 未找到 $File" -ForegroundColor DarkYellow
        return
    }
    Write-Host "`n===== $Name =====" -ForegroundColor Cyan
    $composeArgs = @('-f', $File, $Act) + $Extra
    & docker compose @composeArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[警告] $Name 的 '$Act' 返回非零退出码 $LASTEXITCODE" -ForegroundColor DarkYellow
    }
}

switch ($Action) {
    'up' {
        foreach ($s in $Stacks) { Invoke-Stack $s.Name $s.File 'up' @('-d') }
    }
    'start' {
        foreach ($s in $Stacks) { Invoke-Stack $s.Name $s.File 'start' @() }
    }
    'stop' {
        # 反序停止：先停业务侧依赖方，再停基础设施
        foreach ($s in ($Stacks | Select-Object -Reverse)) { Invoke-Stack $s.Name $s.File 'stop' @() }
    }
    'down' {
        # 注意：不带 -v，卷保留，数据不会丢
        foreach ($s in ($Stacks | Select-Object -Reverse)) { Invoke-Stack $s.Name $s.File 'down' @() }
    }
    'ps' {
        foreach ($s in $Stacks) { Invoke-Stack $s.Name $s.File 'ps' @() }
    }
    'logs' {
        $extra = if ($Service) { @('-f', $Service) } else { @() }
        # 日志是流式的，只跟本项目的日志（多栈同时流式输出会互相刷屏）
        Invoke-Stack $Stacks[0].Name $Stacks[0].File 'logs' $extra
    }
}

Write-Host "`n完成。" -ForegroundColor Green
