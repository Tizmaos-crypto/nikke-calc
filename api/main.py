"""FastAPI wrapper for Jgaram/nikke-calc — 원본 코드는 건드리지 않고 CLI를 HTTP로 노출.

원본 CLI (`python -m runner.sim ...`)를 그대로 subprocess로 호출해서, 원작자가 옵션을
추가/변경해도 API 스키마만 확장하면 계속 살아남는다.

로컬 실행:
    uvicorn api.main:app --reload --port 8000

Render 배포는 render.yaml 참고.
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TIMEOUT = 60  # 시뮬 1회 상한(초)

# CORS: React 앱 도메인만 허용. 배포 시 환경변수로 오버라이드.
ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get(
        "ALLOWED_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000"
    ).split(",") if o.strip()
]

app = FastAPI(title="nikke-calc API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class SimRequest(BaseModel):
    """단발 시뮬 요청. runner/sim.py의 argparse 옵션과 1:1 대응."""
    squad: str = Field(..., description="캐릭터 콤마 구분. 예: '라피 : 레드 후드,크라운'")
    view: str = Field("summary", description="summary|breakdown|analysis|burst|buff|hits|gauge")
    char: Optional[List[str]] = Field(None, description="필터할 캐릭터 이름")
    seed: Optional[int] = None
    expected: bool = False
    no_burst: Optional[str] = None
    duration: Optional[float] = None
    first_burst: float = 3.0
    allow_unparsed: bool = False
    enemy_def: Optional[int] = None
    enemy_code: Optional[str] = None
    core_px: Optional[float] = None
    has_parts: bool = False
    part_break_interval: Optional[float] = None
    burst_gauge_mode: Optional[str] = None
    camera: Optional[str] = None
    camera_mode: Optional[str] = None
    control_mode: Optional[str] = None
    profile: Optional[str] = None
    profile_level: str = "fixed"
    # 반복 옵션 (CLI --tap, --click 등)
    tap: Optional[List[str]] = None
    click: Optional[List[str]] = None
    tactic: Optional[List[str]] = None
    cancel_on_full: Optional[List[str]] = None
    reload_ctrl: Optional[List[str]] = None
    cover_ctrl: Optional[List[str]] = None
    hold_ctrl: Optional[List[str]] = None
    mode_swap: Optional[List[str]] = None
    auto: Optional[List[str]] = None
    favorite: Optional[List[str]] = None
    burst_pattern: Optional[List[str]] = None
    burst_delay: Optional[List[str]] = None


class SimResponse(BaseModel):
    output: str
    stderr: str = ""
    returncode: int
    cmd: List[str]


def _build_cmd(req: SimRequest) -> List[str]:
    cmd = [sys.executable, "-m", "runner.sim", req.squad, "--view", req.view]
    if req.seed is not None:
        cmd += ["--seed", str(req.seed)]
    if req.expected:
        cmd += ["--expected"]
    if req.no_burst:
        cmd += ["--no-burst", req.no_burst]
    if req.duration is not None:
        cmd += ["--duration", str(req.duration)]
    if req.first_burst != 3.0:
        cmd += ["--first-burst", str(req.first_burst)]
    if req.allow_unparsed:
        cmd += ["--allow-unparsed"]
    if req.enemy_def is not None:
        cmd += ["--enemy-def", str(req.enemy_def)]
    if req.enemy_code:
        cmd += ["--enemy-code", req.enemy_code]
    if req.core_px is not None:
        cmd += ["--core-px", str(req.core_px)]
    if req.has_parts:
        cmd += ["--has-parts"]
    if req.part_break_interval is not None:
        cmd += ["--part-break-interval", str(req.part_break_interval)]
    if req.burst_gauge_mode:
        cmd += ["--burst-gauge-mode", req.burst_gauge_mode]
    if req.camera is not None:
        cmd += ["--camera", req.camera]
    if req.camera_mode:
        cmd += ["--camera-mode", req.camera_mode]
    if req.control_mode:
        cmd += ["--control-mode", req.control_mode]
    if req.profile:
        cmd += ["--profile", req.profile]
    if req.profile_level != "fixed":
        cmd += ["--profile-level", req.profile_level]
    for c in (req.char or []):
        cmd += ["--char", c]
    for t in (req.tap or []):
        cmd += ["--tap", t]
    for c in (req.click or []):
        cmd += ["--click", c]
    for t in (req.tactic or []):
        cmd += ["--tactic", t]
    for c in (req.cancel_on_full or []):
        cmd += ["--cancel-on-full", c]
    for r in (req.reload_ctrl or []):
        cmd += ["--reload-ctrl", r]
    for c in (req.cover_ctrl or []):
        cmd += ["--cover-ctrl", c]
    for h in (req.hold_ctrl or []):
        cmd += ["--hold-ctrl", h]
    for m in (req.mode_swap or []):
        cmd += ["--mode-swap", m]
    for a in (req.auto or []):
        cmd += ["--auto", a]
    for f in (req.favorite or []):
        cmd += ["--favorite", f]
    for b in (req.burst_pattern or []):
        cmd += ["--burst-pattern", b]
    for b in (req.burst_delay or []):
        cmd += ["--burst-delay", b]
    return cmd


@app.get("/")
def root() -> dict:
    return {
        "name": "nikke-calc API",
        "upstream": "https://github.com/Jgaram/nikke-calc",
        "license": "MIT (C) 2026 Jgaram",
        "endpoints": ["GET /health", "POST /simulate"],
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/simulate", response_model=SimResponse)
def simulate_endpoint(req: SimRequest) -> SimResponse:
    cmd = _build_cmd(req)
    try:
        result = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=DEFAULT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(504, f"시뮬 시간 초과 ({DEFAULT_TIMEOUT}초)")
    except FileNotFoundError as e:
        raise HTTPException(500, f"Python 실행 파일 없음: {e}")

    # returncode 2는 argparse 검증 실패 (스쿼드 이름/옵션 오류) — 4xx로 매핑
    if result.returncode == 2:
        raise HTTPException(400, result.stdout or result.stderr or "시뮬 인자 오류")
    if result.returncode != 0:
        raise HTTPException(500, result.stderr or result.stdout or "시뮬 실행 실패")

    return SimResponse(
        output=result.stdout,
        stderr=result.stderr,
        returncode=result.returncode,
        cmd=cmd,
    )
