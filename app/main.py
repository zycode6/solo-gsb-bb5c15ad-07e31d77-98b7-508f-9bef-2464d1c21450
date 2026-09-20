"""FastAPI application: HTTP language negotiation interpreter."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .negotiation import (
    AcceptLanguageError,
    ParticipantHeaderError,
    SampleHeaderError,
    analyze_q_stability,
    negotiate,
    negotiate_group,
    plan_coverage,
)
from .schemas import (
    CoveragePlanRequest,
    CoveragePlanResponse,
    GroupNegotiationRequest,
    GroupNegotiationResponse,
    HealthResponse,
    NegotiationRequest,
    NegotiationResponse,
    QStabilityResponse,
)
app = FastAPI(
    title="HTTP 语言协商解释 API",
    description=(
        "解析 RFC 9110 Accept-Language（q 权重、q=0 排除、通配符、重复范围），"
        "按 RFC 4647 lookup 逐级截断匹配服务端支持的 BCP 47 标签，"
        "标准 lookup 无可用候选时可沿可选的 locale_fallbacks 有向链回退，"
        "返回选中语言以及每个候选的接受/淘汰轨迹。"
        "另提供 /api/v1/negotiate/group 群体协商：对多位参与者逐人复用相同的"
        "匹配语义构成权重矩阵，具体范围 q=0 全局否决，按最低个人权重 → 总权重 → "
        "服务端顺序选出共同接受的语言。"
    ),
    version=__version__,
)


@app.exception_handler(AcceptLanguageError)
async def accept_language_error_handler(
    request: Request, exc: AcceptLanguageError
) -> JSONResponse:
    # Same 422 envelope shape FastAPI uses for Pydantic validation errors.
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {
                    "loc": ["body", "accept_language"],
                    "msg": str(exc),
                    "type": "accept_language.syntax_error",
                }
            ]
        },
    )


# Pydantic request validation uses FastAPI's built-in 422 handler,
# which returns the standard {"detail": [...]} envelope.


@app.exception_handler(SampleHeaderError)
async def sample_header_error_handler(
    request: Request, exc: SampleHeaderError
) -> JSONResponse:
    # Same 422 envelope shape, but located at the offending sample's
    # array element (FastAPI loc paths use 0-based array indices).
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {
                    "loc": [
                        "body",
                        "samples",
                        exc.position - 1,
                        "accept_language",
                    ],
                    "msg": str(exc),
                    "type": "accept_language.syntax_error",
                }
            ]
        },
    )


@app.exception_handler(ParticipantHeaderError)
async def participant_header_error_handler(
    request: Request, exc: ParticipantHeaderError
) -> JSONResponse:
    # Same 422 envelope shape, but located at the offending participant's
    # array element (FastAPI loc paths use 0-based array indices).
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {
                    "loc": [
                        "body",
                        "participants",
                        exc.position - 1,
                        "accept_language",
                    ],
                    "msg": str(exc),
                    "type": "accept_language.syntax_error",
                }
            ]
        },
    )


@app.get("/health", response_model=HealthResponse, tags=["健康检查"])
async def health() -> HealthResponse:
    """存活/就绪探针。"""
    return HealthResponse(status="ok", service="language-negotiator")


@app.post(
    "/api/v1/negotiate",
    response_model=NegotiationResponse,
    tags=["语言协商"],
    summary="解释 Accept-Language 并选出服务端语言",
)
async def negotiate_language(
    payload: NegotiationRequest,
) -> JSONResponse:
    result = negotiate(
        header=payload.accept_language,
        supported=payload.supported_languages,
        default=payload.default_language,
        locale_fallbacks=payload.locale_fallbacks,
    )
    response = NegotiationResponse.model_validate(result)
    if response.locale_fallback is None:
        # Drop rather than emit null: when locale_fallbacks did not
        # participate the response keeps the exact pre-feature key set.
        payload_body = response.model_dump(exclude={"locale_fallback"})
    else:
        payload_body = response.model_dump()
    return JSONResponse(content=payload_body)


@app.post(
    "/api/v1/negotiate/q-stability",
    response_model=QStabilityResponse,
    tags=["语言协商"],
    summary="单范围偏好权重稳定区间分析",
)
async def negotiate_q_stability(
    payload: NegotiationRequest,
) -> JSONResponse:
    result = analyze_q_stability(
        header=payload.accept_language,
        supported=payload.supported_languages,
        default=payload.default_language,
        locale_fallbacks=payload.locale_fallbacks,
    )
    baseline = NegotiationResponse.model_validate(result["baseline"])
    if baseline.locale_fallback is None:
        # Match /negotiate: the key is dropped rather than emitted null.
        baseline_body = baseline.model_dump(exclude={"locale_fallback"})
    else:
        baseline_body = baseline.model_dump()
    body = {
        "baseline": baseline_body,
        "ranges": result["ranges"],
    }
    return JSONResponse(content=body)


@app.post(
    "/api/v1/negotiate/group",
    response_model=GroupNegotiationResponse,
    tags=["语言协商"],
    summary="多参与者群体语言协商",
)
async def negotiate_group_language(
    payload: GroupNegotiationRequest,
) -> GroupNegotiationResponse:
    result = negotiate_group(
        participants=[(p.id, p.accept_language) for p in payload.participants],
        supported=payload.supported_languages,
        default=payload.default_language,
    )
    return GroupNegotiationResponse.model_validate(result)


@app.post(
    "/api/v1/negotiate/coverage-plan",
    response_model=CoveragePlanResponse,
    tags=["语言协商"],
    summary="候选语言覆盖规划",
)
async def negotiate_coverage_plan(
    payload: CoveragePlanRequest,
) -> CoveragePlanResponse:
    result = plan_coverage(
        samples=[(s.accept_language, s.traffic) for s in payload.samples],
        supported=payload.supported_languages,
        default=payload.default_language,
        candidates=payload.candidates,
        max_additions=payload.max_additions,
    )
    return CoveragePlanResponse.model_validate(result)


@app.get("/", tags=["元信息"])
async def root() -> dict[str, str]:
    return {
        "service": "language-negotiator",
        "version": __version__,
        "docs": "/docs",
        "health": "/health",
        "negotiate": "POST /api/v1/negotiate",
        "negotiate_q_stability": "POST /api/v1/negotiate/q-stability",
        "negotiate_group": "POST /api/v1/negotiate/group",
    }
