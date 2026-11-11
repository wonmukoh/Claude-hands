# claude-hands 를 레지스트리 없이 까는 묶음 굽기
#   powershell -ExecutionPolicy Bypass -File build.ps1
# 결과: dist\claude-hands\  (python\python.exe + python\Lib\site-packages)
#       dist\claude-hands-<판>.zip
#
# 받는 PC 에는 Python·pip·인터넷이 없어도 된다. 폴더를 풀고 아래처럼 등록한다.
#   claude mcp add -s user claude-hands -- "<폴더>\python\python.exe" -m claude_hands.server
#
# 왜 PyInstaller exe 가 아닌가 (2026-11-11 실측): 이 PC 는 스마트 앱 컨트롤이 켜져 있고,
# 새로 구운 서명 없는 exe 는 평판이 없어 「애플리케이션 제어 정책」으로 막혔다
# (CodeIntegrity 3077, 정책 {0283ac0f-…}). 이 묶음에는 새로 만든 실행 파일이 하나도 없다 —
# python.exe·python3xx.dll 은 python.org 가 서명한 것, .pyd·.dll 은 PyPI 휠의 것 그대로라
# 지금 pip 로 깐 PC 에서 이미 돌고 있는 바로 그 파일들이다. 우리가 넣는 것은 .py 뿐이다.
#
# 컴파일된 휠(pydantic-core·pywin32·pillow)이 Python 판마다 다른 문제는, 묶음이 자기
# Python 을 들고 다니고 휠을 그 판(cp3xx win_amd64)에 맞춰 받으므로 생기지 않는다.

param([string]$Version = '')

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not $Version) { $Version = (& py -3.14 -c "import platform; print(platform.python_version())").Trim() }
$short = ($Version -split '\.')[0..1] -join ''          # 3.14.10 → 314
$hands = (& py -3.14 -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])").Trim()

$build = Join-Path $PSScriptRoot 'build'
$out = Join-Path $PSScriptRoot 'dist\claude-hands'
$pyDir = Join-Path $out 'python'
$site = Join-Path $pyDir 'Lib\site-packages'
New-Item -ItemType Directory -Force -Path $build | Out-Null

# 1. python.org 임베드용 Python (굽는 PC 에서만 받는다)
$embedZip = Join-Path $build "python-$Version-embed-amd64.zip"
if (-not (Test-Path $embedZip)) {
    Invoke-WebRequest -UseBasicParsing -Uri "https://www.python.org/ftp/python/$Version/python-$Version-embed-amd64.zip" -OutFile $embedZip
}
if (Test-Path $out) { Remove-Item -Recurse -Force $out }
Expand-Archive -Path $embedZip -DestinationPath $pyDir
$sig = Get-AuthenticodeSignature (Join-Path $pyDir 'python.exe')
if ($sig.Status -ne 'Valid') { throw "python.exe 서명이 올바르지 않습니다: $($sig.Status)" }

# 임베드용은 site 를 끄고 정해진 경로만 본다. site-packages 를 더하고 site 를 켜야
# pywin32 의 .pth(DLL 경로 잡기)가 돈다. ._pth 가 있으면 PYTHONPATH·사용자 site 는 무시된다
# — 그 PC 에 깔린 다른 Python 의 꾸러미가 섞여 들지 않는다.
$pth = Join-Path $pyDir "python$short._pth"
@("python$short.zip", '.', 'Lib\site-packages', 'import site') | Set-Content -Encoding ascii $pth

# 2. 이 저장소를 휠로, 의존 꾸러미는 그 Python 판에 맞는 휠로만 받아 넣는다
$venv = Join-Path $build 'venv'
$vpy = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $vpy)) { & py -3.14 -m venv $venv; if ($LASTEXITCODE -ne 0) { throw '가상환경 실패' } }
& $vpy -m pip install --quiet --upgrade pip wheel
$wheels = Join-Path $build 'wheels'
if (Test-Path $wheels) { Remove-Item -Recurse -Force $wheels }
& $vpy -m pip wheel --quiet --no-deps -w $wheels .
if ($LASTEXITCODE -ne 0) { throw '휠 만들기 실패' }
$wheel = (Get-ChildItem $wheels -Filter 'claude_hands-*.whl' | Select-Object -First 1).FullName
& $vpy -m pip install --quiet --target $site --only-binary=:all: `
    --platform win_amd64 --python-version $Version --implementation cp `
    $wheel
if ($LASTEXITCODE -ne 0) { throw '의존 꾸러미 받기 실패' }
Remove-Item -Recurse -Force (Join-Path $site 'bin') -ErrorAction SilentlyContinue   # --target 이 만든 pip 식 실행기 (쓰지 않음)
Remove-Item -Force (Join-Path $site 'PyWin32.chm') -ErrorAction SilentlyContinue      # 도움말 파일 2.5MB

# 3. UIA 래퍼를 미리 만들고 .pyc 를 미리 굽는다. 받는 PC 의 설치 폴더에 쓸 권한이 없어도
#    처음 뜰 때 아무것도 쓰지 않아도 되게.
$bundled = Join-Path $pyDir 'python.exe'
& $bundled -c "import comtypes.client as c; c.GetModule('UIAutomationCore.dll'); import comtypes.gen.UIAutomationClient"
if ($LASTEXITCODE -ne 0) { throw 'UIA 래퍼 만들기 실패' }
& $bundled -m compileall -q -j 0 $site | Out-Null

# 4. 점검: 빈 임시 폴더로 옮겨, 이 PC 의 Python 과 환경 변수 없이 띄운다
& $vpy packaging\smoke.py $out
if ($LASTEXITCODE -ne 0) { throw '묶음 점검 실패' }

$zip = Join-Path $PSScriptRoot "dist\claude-hands-$hands.zip"
if (Test-Path $zip) { Remove-Item -Force $zip }
Compress-Archive -Path $out -DestinationPath $zip
$size = (Get-ChildItem -Recurse -File $out | Measure-Object Length -Sum).Sum / 1MB
Write-Host ('완료: {0} ({1:N1} MB 풀린 크기, zip {2:N1} MB)' -f $out, $size, ((Get-Item $zip).Length / 1MB))
