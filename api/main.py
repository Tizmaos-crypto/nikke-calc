"""FastAPI wrapper for Jgaram/nikke-calc — 원본 코드는 건드리지 않고 CLI를 HTTP로 노출.

원본 CLI (`python -m runner.sim ...`)를 그대로 subprocess로 호출해서, 원작자가 옵션을
추가/변경해도 API 스키마만 확장하면 계속 살아남는다.

`profile_data`는 OS 임시 요청 파일의 인라인 dict로 전달한다. 서버 프로필 선택은 금지한다.

로컬 실행:
    uvicorn api.main:app --reload --port 8000

Render 배포는 render.yaml 참고.
"""
from __future__ import annotations

import hashlib
import copy
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import tempfile
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict, model_validator
from api.security import RequestLimits

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE_DIR = Path(REPO_ROOT) / "profiles"
DEFAULT_TIMEOUT = 60  # 시뮬 1회 상한(초)


def _engine_fingerprint(repo_root: Path) -> str:
    """Identify calculation inputs, including local changes, without personal data."""
    root = Path(repo_root)
    files = set()
    for folder, pattern in (("data", "*.json"), ("calculator", "*.py"), ("runner", "*.py")):
        files.update(path for path in (root / folder).rglob(pattern)
                     if path.is_file() and "__pycache__" not in path.parts)
    wrapper = root / "api" / "main.py"
    if wrapper.is_file():
        files.add(wrapper)  # Request-to-CLI mapping can change calculated results.
    files.update((root / "api").glob("*.py"))
    digest = hashlib.sha256(b"nikke-calc-engine-v1\0")
    for path in sorted(files, key=lambda value: value.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return "sha256:" + digest.hexdigest()


# Compute once at process startup. /health stays inexpensive, and a deployment
# or a rollback cannot reuse calibration cached under different engine contents.
_ENGINE_COMMIT = _engine_fingerprint(Path(REPO_ROOT))
_SOURCE_COMMIT = os.environ.get("RENDER_GIT_COMMIT", "").strip()


# The production UI remains allowed when an older environment only lists local clients.
# ALLOWED_ORIGINS supplies additional deployment/development origins.
def _cors_origins(configured: str | None) -> list[str]:
    configured = configured if configured is not None else "http://localhost:3000,http://127.0.0.1:3000"
    return list(dict.fromkeys(["https://nikke-raid.vercel.app", *(
        origin.strip() for origin in configured.split(",") if origin.strip()
    )]))


ALLOWED_ORIGINS = _cors_origins(os.environ.get("ALLOWED_ORIGINS"))


app = FastAPI(title="nikke-calc API", version="0.3.0")  # v0.3: dildoro boss share proxy
app.add_middleware(RequestLimits)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def invalid_request(_request, exc):
    # Do not echo the submitted profile or non-JSON numbers in validation errors.
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": error["loc"], "msg": error["msg"]} for error in exc.errors()
    ]})


class SimRequest(BaseModel):
    """단발 시뮬 요청. runner/sim.py의 argparse 옵션과 1:1 대응."""
    model_config = ConfigDict(allow_inf_nan=False)
    squad: str = Field(..., description="캐릭터 콤마 구분. 예: '라피 : 레드 후드,크라운'")
    view: str = Field("summary", description="summary|breakdown|analysis|burst|buff|hits|gauge")
    char: Optional[List[str]] = Field(None, description="필터할 캐릭터 이름")
    seed: Optional[int] = None
    expected: bool = False
    no_burst: Optional[str] = None
    duration: Optional[float] = Field(None, gt=0, le=600)
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
    profile: Optional[str] = None                       # 레거시 필드: 공개 API에서는 항상 거부
    profile_data: Optional[dict] = Field(None,          # v2: 요청 시점에 프로필 dict 자체 전달
        description="인라인 프로필 딕셔너리 (chars/_account/_meta). 개인 프로필 폴더를 읽거나 쓰지 않음")
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

    @model_validator(mode="after")
    def public_api_limits(self):
        if self.profile_data is not None:
            try:
                json.dumps(self.profile_data, allow_nan=False)
            except (ValueError, TypeError):
                raise ValueError("profile_data는 유한한 숫자를 포함한 JSON이어야 합니다.") from None
        if self.profile is not None:
            raise ValueError("서버 프로필 이름/경로는 사용할 수 없습니다. profile_data를 사용하세요.")
        for value in self.model_dump(exclude={"profile_data"}).values():
            items = value if isinstance(value, list) else [value]
            if len(items) > 32:
                raise ValueError("옵션 항목은 최대 32개입니다.")
            if any(isinstance(item, str) and (len(item) > 512 or item.startswith("-") or "\x00" in item) for item in items):
                raise ValueError("옵션 문자열 형식 또는 길이를 확인하세요.")
        if not 1 <= len(self.squad.split(",")) <= 5:
            raise ValueError("스쿼드는 1~5명이어야 합니다.")
        return self


class SimResponse(BaseModel):
    engine: str = _ENGINE_COMMIT  # Identity of the process which produced this output.
    output: str
    stderr: str = ""
    returncode: int
    cmd: List[str]
    profile_used: Optional[str] = None  # v2: 어떤 프로필 이름으로 돌았는지 (디버깅용)


def _build_cmd(req: SimRequest, profile_name_override: Optional[str] = None) -> List[str]:
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
    # Only a server-created request file is accepted, never a client path.
    profile_name = profile_name_override
    if profile_name:
        cmd += ["--request", profile_name]
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


def _write_temp_profile(profile_data: dict) -> tuple[str, Path]:
    """Transport an inline profile in a private OS temp file, never profiles/."""
    # Older web/collector clients attach a display-only gear label. The calculator
    # never consumes it; remove only this known label before strict validation.
    profile_data = copy.deepcopy(profile_data)
    chars = profile_data.get("chars")
    if isinstance(chars, dict):
        for entry in chars.values():
            gear = entry.get("equipment") if isinstance(entry, dict) else None
            if isinstance(gear, dict):
                for part in gear.values():
                    if isinstance(part, dict):
                        part.pop("_track", None)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", prefix="nikke-request-",
                                     encoding="utf-8", delete=False) as handle:
        path = Path(handle.name)
        try:
            json.dump({"profile": profile_data}, handle, ensure_ascii=False, allow_nan=False)
        except Exception:
            handle.close()
            path.unlink(missing_ok=True)
            raise
    return str(path), path


@app.get("/")
def root() -> dict:
    return {
        "name": "nikke-calc API",
        "version": "0.3.0",
        "upstream": "https://github.com/Jgaram/nikke-calc",
        "license": "MIT (C) 2026 Jgaram",
        "endpoints": ["GET /health", "POST /simulate", "GET /dildoro/boss?c=<code>"],
        "features": ["profile_data in request body (v0.2.0)", "dildoro boss share proxy (v0.3.0)"],
    }


@app.get("/health")
def health() -> dict:
    result = {"status": "ok", "version": "0.3.0", "commit": _ENGINE_COMMIT}
    if _SOURCE_COMMIT:
        result["source_commit"] = _SOURCE_COMMIT
    return result


# ─────────────────────────────────────────────────────────────
# dildoro 보스 공유 프록시 (v0.3.0)
# 브라우저는 dildoro.com에 CORS로 막혀서 우리 백엔드가 대신 부른다.
# 우리 백엔드의 CORS는 우리 앱 도메인만 허용 (ALLOWED_ORIGINS).
# ─────────────────────────────────────────────────────────────
_DILDORO_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


@app.get("/dildoro/boss")
def dildoro_boss_proxy(c: str) -> dict:
    """dildoro.com/api/boss?c=<code> 응답을 그대로 반환.

    유저가 웹앱에서 공유 코드/URL을 붙여넣으면 프론트가 이 endpoint를 호출.
    반환된 JSON은 프론트에서 파싱해 우리 스키마로 매핑.
    """
    if not _DILDORO_CODE_RE.match(c or ""):
        raise HTTPException(400, "잘못된 공유 코드 형식")
    url = f"https://dildoro.com/api/boss?c={c}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; nikke-calc-api-proxy/0.3.0)",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            body = res.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise HTTPException(e.code, f"dildoro 응답 오류: HTTP {e.code}")
    except urllib.error.URLError as e:
        raise HTTPException(502, f"dildoro 접속 실패: {e.reason}")
    except Exception as e:
        raise HTTPException(500, f"프록시 오류: {e}")

    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise HTTPException(502, f"dildoro 응답 JSON 파싱 실패: {e}")


@app.post("/simulate", response_model=SimResponse)
def simulate_endpoint(req: SimRequest) -> SimResponse:
    # v2: profile_data 있으면 임시 파일 만들고, finally에서 삭제
    temp_profile_name: Optional[str] = None
    temp_profile_path: Optional[Path] = None
    if req.profile_data is not None:
        try:
            temp_profile_name, temp_profile_path = _write_temp_profile(req.profile_data)
        except Exception:
            raise HTTPException(500, "프로필 요청 준비 실패") from None

    try:
        cmd = _build_cmd(req, profile_name_override=temp_profile_name)
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
        except OSError:
            raise HTTPException(500, "시뮬 실행 환경 오류") from None

        # returncode 2는 argparse 검증 실패 (스쿼드 이름/옵션 오류) — 4xx로 매핑
        if result.returncode == 2:
            raise HTTPException(400, result.stdout or result.stderr or "시뮬 인자 오류")
        if result.returncode != 0:
            raise HTTPException(500, "시뮬 실행 실패: 입력값을 확인하세요.")

        return SimResponse(
            output=result.stdout,
            stderr="",
            returncode=result.returncode,
            cmd=[],  # Do not return executable paths or private request filenames.
            profile_used="inline" if temp_profile_path else None,
        )
    finally:
        # 임시 프로필 파일 정리 (에러 있어도 삭제)
        if temp_profile_path and temp_profile_path.exists():
            try:
                temp_profile_path.unlink()
            except Exception:
                pass  # 정리 실패는 무시 (다음 요청에 영향 없음)
