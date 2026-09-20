"""Pydantic request/response models for the language negotiation API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .negotiation import (
    LocaleFallbackError,
    TAG_RE,
    normalize_fallbacks,
    validate_fallback_chains,
)


def _check_unique_tags(value: list[str], field: str) -> list[str]:
    normalized_seen: set[str] = set()
    for tag in value:
        if not isinstance(tag, str) or not TAG_RE.fullmatch(tag):
            raise ValueError(f"不是合法的 BCP 47 标签: {tag!r}")
        key = tag.lower()
        if key in normalized_seen:
            raise ValueError(f"{field} 存在重复标签（大小写不敏感）: {tag!r}")
        normalized_seen.add(key)
    return value


def _check_supported_tags(value: list[str]) -> list[str]:
    return _check_unique_tags(value, "supported_languages")


def _check_default_tag(value: str) -> str:
    if not TAG_RE.fullmatch(value):
        raise ValueError(f"default_language 不是合法的 BCP 47 标签: {value!r}")
    return value


def _check_default_in_supported(supported: list[str], default: str) -> None:
    supported_lower = {tag.lower() for tag in supported}
    if default.lower() not in supported_lower:
        raise ValueError(
            "default_language 必须包含在 supported_languages 中（大小写不敏感）"
        )


class NegotiationRequest(BaseModel):
    """Negotiation input.

    ``accept_language`` is the raw header value; ``null``/missing or an
    empty string means "no valid ranges" and triggers the default
    fallback. ``locale_fallbacks`` is an optional directed-chain mapping
    of source BCP 47 tag -> next BCP 47 tag consulted only when standard
    lookup has no acceptable candidate.
    """

    model_config = ConfigDict(extra="forbid")

    accept_language: str | None = Field(
        default=None,
        title="原始 Accept-Language 头",
        description="例如 'zh-CN,zh;q=0.9,en;q=0.8,*;q=0.5'；缺失/空串视为无有效范围",
    )
    supported_languages: list[str] = Field(
        min_length=1,
        title="服务端支持的 BCP 47 语言标签（按优先级/稳定顺序排列）",
    )
    default_language: str = Field(
        title="默认语言标签，必须包含在 supported_languages 中"
    )
    locale_fallbacks: dict[str, str] | None = Field(
        default=None,
        title="可选的语言回退有向链（源标签 -> 下一标签）",
        description=(
            "仅当标准 lookup 没有可接受候选时启用：按正 q 具体范围权重降序、"
            "头部顺序依次沿链查找首个受支持语言；被任一 q=0 具体范围排除的"
            "候选不会被链选中。源标签不可重复（大小写不敏感），标签必须合法，"
            "不允许环路，且每条链必须能到达某个受支持语言。"
        ),
    )

    @field_validator("supported_languages")
    @classmethod
    def _validate_supported(cls, value: list[str]) -> list[str]:
        return _check_supported_tags(value)

    @field_validator("default_language")
    @classmethod
    def _validate_default(cls, value: str) -> str:
        return _check_default_tag(value)

    @field_validator("locale_fallbacks", mode="before")
    @classmethod
    def _validate_fallbacks_shape(cls, value: Any) -> Any:
        if value is None:
            return None
        try:
            return normalize_fallbacks(value)
        except LocaleFallbackError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("locale_fallbacks")
    @classmethod
    def _validate_fallback_chains(
        cls, value: dict[str, str] | None, info: ValidationInfo
    ) -> dict[str, str] | None:
        # Runs after supported_languages/default_language validators
        # (field declaration order), so topology errors are located at
        # ["body", "locale_fallbacks"].
        if value is not None and info.data.get("supported_languages"):
            try:
                validate_fallback_chains(value, info.data["supported_languages"])
            except LocaleFallbackError as exc:
                raise ValueError(str(exc)) from exc
        return value

    @model_validator(mode="after")
    def _default_must_be_supported(self) -> "NegotiationRequest":
        _check_default_in_supported(self.supported_languages, self.default_language)
        return self


class TraceEvent(BaseModel):
    truncated_to: str = Field(description="逐级截断后的标签前缀，或 '*' 通配符")
    outcome: Literal["match", "no-match", "skipped"]
    matched_range: str | None = None
    q: float | None = None
    header_position: int | None = None
    detail: str | None = None


class CandidateReport(BaseModel):
    tag: str
    supported_index: int
    truncation_chain: list[str]
    events: list[TraceEvent]
    matched_range: str | None
    q: float | None = Field(description="命中范围的客户端权重；未命中为 null")
    status: Literal["selected", "eligible", "excluded", "unmatched", "no-range"]
    detail: str


class ParsedRange(BaseModel):
    range: str
    q: float
    position: int = Field(description="在 Accept-Language 头中的序号（从 1 开始）")


class IgnoredDuplicate(BaseModel):
    range: str
    position: int


class LocaleFallbackReport(BaseModel):
    triggered: bool = Field(description="是否由 locale_fallbacks 链选中语言")
    source_range: str = Field(
        description="触发回退的客户端具体范围（小写、去重后的范围）"
    )
    source_q: float = Field(description="触发范围的客户端权重（严格为正）")
    source_position: int = Field(description="触发范围在 Accept-Language 头中的序号")
    path: list[str] = Field(
        description="实际走过的有向链路径（小写标签，含源与终点）"
    )
    selected_language: str = Field(description="链最终命中的受支持标签（服务端原始大小写）")


class NegotiationResponse(BaseModel):
    selected_language: str
    selected_supported_index: int
    fallback: bool
    fallback_reason: Literal["no-valid-range", "no-acceptable-candidate"] | None
    message: str
    parsed_ranges: list[ParsedRange]
    ignored_duplicates: list[IgnoredDuplicate]
    candidates: list[CandidateReport]
    # Present only when a locale_fallbacks chain actually selected the
    # language; the endpoint drops this key entirely when unused, so
    # requests without locale_fallbacks keep the exact legacy payload.
    locale_fallback: LocaleFallbackReport | None = None


class QStabilityBoundaryPoint(BaseModel):
    """Outcome at the first legal q grid point just outside a stable interval."""

    q: float = Field(description="区间外相邻网格点的权重（三位小数）")
    selected_language: str = Field(description="该网格点重新协商后选中的语言")
    fallback: bool = Field(description="该网格点重新协商后是否回退到默认语言")


class QStabilityRangeReport(BaseModel):
    """Stable-q interval for one de-duplicated language range."""

    range: str = Field(description="去重后的语言范围（小写；可能为 '*' 通配符）")
    position: int = Field(
        description="该范围在原始 Accept-Language 头中的位置（从 1 开始）"
    )
    original_q: float = Field(description="该范围在原始请求中的 q（必在稳定区间内）")
    lower_bound: float = Field(
        description="保持 selected_language 与 fallback 均不变的最大连续区间下界（含）"
    )
    upper_bound: float = Field(
        description="保持 selected_language 与 fallback 均不变的最大连续区间上界（含）"
    )
    lower_outside: QStabilityBoundaryPoint | None = Field(
        description="紧邻下界之外的网格点结果；下界已是 0.000 时为 null"
    )
    upper_outside: QStabilityBoundaryPoint | None = Field(
        description="紧邻上界之外的网格点结果；上界已是 1.000 时为 null"
    )


class QStabilityResponse(BaseModel):
    baseline: NegotiationResponse = Field(
        description="按现有规则得到的基准单人协商结果（与 /negotiate 完全一致）"
    )
    ranges: list[QStabilityRangeReport] = Field(
        description="去重后每个语言范围各自的权重稳定区间（按头部顺序）"
    )


class GroupParticipant(BaseModel):
    """One group member: a unique id plus their raw Accept-Language header."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        min_length=1,
        title="参与者唯一标识",
        description="请求内不可重复（精确匹配，区分大小写）",
    )
    accept_language: str | None = Field(
        default=None,
        title="该参与者的原始 Accept-Language 头",
        description="缺失/null/空白表示该参与者接受任意语言（弃权，不参与否决）",
    )


class GroupNegotiationRequest(BaseModel):
    """Group negotiation input.

    ``participants`` is a non-empty list of members, each with a unique
    ``id`` and an optional ``accept_language`` header. Every participant
    is evaluated with the same RFC 9110/4647 lookup semantics as the
    single negotiation endpoint; the resulting weight matrix decides one
    language acceptable to the whole group.
    """

    model_config = ConfigDict(extra="forbid")

    participants: list[GroupParticipant] = Field(
        min_length=1,
        title="参与者列表（至少 1 人，id 不可重复）",
    )
    supported_languages: list[str] = Field(
        min_length=1,
        title="服务端支持的 BCP 47 语言标签（按优先级/稳定顺序排列）",
    )
    default_language: str = Field(
        title="默认语言标签，必须包含在 supported_languages 中"
    )

    @field_validator("participants")
    @classmethod
    def _validate_unique_ids(
        cls, value: list[GroupParticipant]
    ) -> list[GroupParticipant]:
        seen: set[str] = set()
        for participant in value:
            if participant.id in seen:
                raise ValueError(
                    f"participants 存在重复 id: {participant.id!r}"
                )
            seen.add(participant.id)
        return value

    @field_validator("supported_languages")
    @classmethod
    def _validate_supported(cls, value: list[str]) -> list[str]:
        return _check_supported_tags(value)

    @field_validator("default_language")
    @classmethod
    def _validate_default(cls, value: str) -> str:
        return _check_default_tag(value)

    @model_validator(mode="after")
    def _default_must_be_supported(self) -> "GroupNegotiationRequest":
        _check_default_in_supported(self.supported_languages, self.default_language)
        return self


class ParticipantWeight(BaseModel):
    participant_id: str
    q: float = Field(
        description="该参与者对此候选的权重（未命中计 0；弃权参与者计 1）"
    )
    matched_range: str | None = Field(
        description="该参与者实际命中的语言范围（小写）；弃权或未命中为 null"
    )
    veto: bool = Field(description="该参与者是否以具体范围 q=0 否决此候选")


class GroupCandidateReport(BaseModel):
    tag: str
    supported_index: int
    min_q: float = Field(description="所有参与者中的最低个人权重")
    total_q: float = Field(description="所有参与者权重之和")
    vetoed: bool = Field(description="是否被至少一位参与者以具体范围 q=0 否决")
    vetoed_by: list[str] = Field(
        description="以具体范围 q=0 否决此候选的参与者 id（按 participants 顺序）"
    )
    status: Literal["selected", "eligible", "vetoed", "unacceptable"]
    weights: list[ParticipantWeight]


class GroupParticipantReport(BaseModel):
    id: str
    position: int = Field(description="在 participants 数组中的序号（从 1 开始）")
    abstained: bool = Field(
        description="是否未提供有效 Accept-Language 头（视为接受任意语言）"
    )
    parsed_ranges: list[ParsedRange]
    ignored_duplicates: list[IgnoredDuplicate]


class GroupNegotiationResponse(BaseModel):
    selected_language: str
    selected_supported_index: int
    fallback: bool
    fallback_reason: Literal["no-common-acceptable-candidate"] | None
    message: str
    participants: list[GroupParticipantReport]
    candidates: list[GroupCandidateReport]


class CoverageSample(BaseModel):
    """One traffic-weighted Accept-Language sample for coverage planning."""

    model_config = ConfigDict(extra="forbid")

    accept_language: str | None = Field(
        default=None,
        title="该样本的原始 Accept-Language 头",
        description="缺失/null/空白视为无有效范围（恒定走默认兜底，无法被改善）",
    )
    traffic: float = Field(
        ge=0,
        allow_inf_nan=False,
        title="该样本代表的流量",
        description="非负数值；规划按流量求和评估各候选子集",
    )


class CoveragePlanRequest(BaseModel):
    """Coverage planning input.

    ``samples`` are traffic-weighted Accept-Language headers evaluated
    with the exact semantics of the single negotiation endpoint;
    ``candidates`` are 1-12 new languages under consideration and
    ``max_additions`` caps how many of them a plan may add (1-3).
    """

    model_config = ConfigDict(extra="forbid")

    supported_languages: list[str] = Field(
        min_length=1,
        title="当前服务端支持的 BCP 47 语言标签（按优先级/稳定顺序排列）",
    )
    default_language: str = Field(
        title="默认语言标签，必须包含在 supported_languages 中"
    )
    samples: list[CoverageSample] = Field(
        min_length=1,
        title="带非负 traffic 的 Accept-Language 样本（至少 1 个）",
    )
    candidates: list[str] = Field(
        min_length=1,
        max_length=12,
        title="候选新增语言（1～12 个，不可重复，且不得已在支持列表中）",
    )
    max_additions: int = Field(
        ge=1,
        le=3,
        title="最多新增语言数（1～3）",
    )

    @field_validator("supported_languages")
    @classmethod
    def _validate_supported(cls, value: list[str]) -> list[str]:
        return _check_supported_tags(value)

    @field_validator("default_language")
    @classmethod
    def _validate_default(cls, value: str) -> str:
        return _check_default_tag(value)

    @field_validator("candidates")
    @classmethod
    def _validate_candidates(cls, value: list[str]) -> list[str]:
        return _check_unique_tags(value, "candidates")

    @model_validator(mode="after")
    def _default_must_be_supported(self) -> "CoveragePlanRequest":
        _check_default_in_supported(self.supported_languages, self.default_language)
        return self

    @model_validator(mode="after")
    def _candidates_must_extend_supported(self) -> "CoveragePlanRequest":
        supported_lower = {tag.lower() for tag in self.supported_languages}
        for tag in self.candidates:
            if tag.lower() in supported_lower:
                raise ValueError(
                    f"candidates 与 supported_languages 重复（大小写不敏感）: {tag!r}"
                )
        return self


class CoverageSamplePlan(BaseModel):
    position: int = Field(description="在 samples 数组中的序号（从 1 开始）")
    traffic: float = Field(description="该样本的流量（原样回显）")
    baseline_selected_language: str = Field(
        description="规划前（现有支持列表）选中的语言"
    )
    baseline_fallback: bool = Field(description="规划前是否回退到默认语言")
    planned_selected_language: str = Field(
        description="按选中方案新增语言后选中的语言"
    )
    planned_fallback: bool = Field(
        description="按选中方案新增语言后是否仍回退到默认语言"
    )
    improved: bool = Field(description="是否由默认兜底转为正常命中")


class CoveragePlanResponse(BaseModel):
    selected_additions: list[str] = Field(
        description="建议新增的语言（按候选输入顺序；空列表表示无需新增）"
    )
    baseline_fallback_traffic: float = Field(
        description="现有支持列表下回退到默认语言的总流量"
    )
    improved_traffic: float = Field(
        description="按选中方案新增语言后可从默认兜底转为正常命中的流量"
    )
    message: str
    samples: list[CoverageSamplePlan]


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str
