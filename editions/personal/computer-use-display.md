# Computer-Use Display Standard (Personal)

> 권장 표준 해상도: **1280x800**. 좌표 클릭의 기준이 되는 화면 크기를 고정해야
> 캡처 좌표와 실제 클릭 위치가 어긋나지 않는다.

## 왜 고정하는가

- 캡처 이미지 크기와 X11 윈도우 크기가 다르면 클릭이 빗나간다.
- 실측 사례: 1440x900 화면에서 1050x808 윈도우 캡처 기준 클릭이
  약 100px 위로 어긋나 비밀번호란 입력에 반복 실패했다.
- 해상도를 1280x800으로 고정하고 브라우저 창을 화면 안에 맞추면 1:1 매핑된다.

## 적용 (Linux Xvnc Bot Desktop 예시)

```bash
# 1) 모드 등록 (1회) — 표준 1280x800@60
xrandr --display :20 --newmode "1280x800_60" 83.50 1280 1352 1480 1680 \
  800 803 809 831 -hsync +vsync
xrandr --display :20 --addmode VNC-0 "1280x800_60"

# 2) 전환
xrandr --display :20 --output VNC-0 --mode "1280x800_60"

# 3) 브라우저 창을 화면 안에 맞춤 (예: 1270x760)
wmctrl -r "<window title>" -e 0,0,0,1270,760
```

## 확인

```bash
xrandr --display :20 | head -n 2   # current 1280 x 800 확인
wmctrl -lG | grep -i chrom         # 창 크기 확인
```

## 관련

- 자격 증명 입력 opt-in: `editions/personal/computer-use-policy.sh`
  (설계서 `docs/computer-use-credential-typing-v1.0.md`)
- 비밀번호·결제·2FA는 기본 금지. 소유자 명시적 opt-in 후에만 입력한다.
