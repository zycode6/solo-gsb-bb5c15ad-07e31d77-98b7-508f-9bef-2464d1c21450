"""Accept-Language parsing and language-range negotiation.

Implements the negotiation rules required by the service:

* q-weight parsing per RFC 9110 (``Accept-Language``), including ``q=0``
  exclusion, the wildcard range ``*`` and duplicated ranges;
* lookup style matching (RFC 4647 section 3.4): a candidate tag is truncated
  one subtag at a time (``zh-Hant-TW`` -> ``zh-Hant`` -> ``zh``) until a
  configured language range matches;
* concrete ranges always take precedence over the wildcard;
* candidates are ranked by the client q weight and ties are broken by the
  stable order of the server-supported list;
* empty / absent header falls back to the configured default language;
* malformed header syntax raises :class:`AcceptLanguageError`;
* when standard lookup yields no acceptable candidate, optional directed
  ``locale_fallbacks`` chains (source tag -> next tag) are walked starting
  from the client's positive-q concrete ranges, in descending q / header
  order; a chain node that is a supported tag wins, unless it is excluded
  by any concrete q=0 range, in which case the walk continues;
* :func:`negotiate_group` reuses the exact same per-person matching
  semantics for several participants at once: the per-candidate weights
  form a participants x candidates matrix, any candidate excluded by a
  participant's concrete ``q=0`` range is globally vetoed, and the winner
  is chosen by highest minimum personal weight, then highest total
  weight, then server-list order;
* :func:`plan_coverage` reuses the exact same per-request negotiation
  semantics for capacity planning: every traffic-weighted
  Accept-Language sample is negotiated against the current supported
  list (baseline) and against that list extended by each candidate
  subset of up to ``max_additions`` new languages; the subset moving
  the most traffic from the default fallback to a normal hit wins,
  ties broken by fewer additions, then by earlier candidate input
  order.
* :func:`analyze_q_stability` also reuses :func:`negotiate`: after a
  baseline run it scans, for every de-duplicated range individually,
  the full three-decimal q grid (``0.000``..``1.000``) while the other
  ranges stay fixed, and reports the maximal contiguous grid interval
  around the original q that keeps ``selected_language`` and the
  fallback state unchanged.

Language tag grammar follows RFC 5646 (BCP 47) primary tag grammar, which
covers every ordinary language tag (the legacy "grandfathered" irregular
tags from the RFC registry are intentionally rejected).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import combinations
from typing import Any

# RFC 9110 quality values carry at most three fractional digits, so the
# legal q grid has 1001 points from 0.000 through 1.000 inclusive.
Q_GRID_STEPS = 1000

# RFC 5646 section 2.1 grammar (main tag grammar, no grandfathered tags).
_PRIVATEUSE = r"x(?:-[0-9a-z]{1,8})+"
_LANGUAGE_TAG = (
    r"(?:[a-z]{2,3}(?:-[a-z]{3}){0,3}"  # 2/3 letter language + extlangs
    r"|[a-z]{4,8})"  # 4+ letter language
    r"(?:-[a-z]{4})?"  # script
    r"(?:-(?:[a-z]{2}|[0-9]{3}))?"  # region
    r"(?:-(?:[0-9a-z]{5,8}|[0-9][0-9a-z]{3}))*"  # variants
    r"(?:-[0-9a-wy-z](?:-[0-9a-z]{2,8})+)*"  # extensions
    r"(?:-x(?:-[0-9a-z]{1,8})+)?"  # private use
)
TAG_RE = re.compile(rf"(?:{_LANGUAGE_TAG}|{_PRIVATEUSE})", re.IGNORECASE)
_RANGE_RE = re.compile(rf"^(?:\*|{_LANGUAGE_TAG}|{_PRIVATEUSE})$", re.IGNORECASE)

# RFC 9110 quality value: 0 / 1 / three-decimal fractions ("q=.5" is invalid).
_QVALUE_RE = re.compile(r"^(?:0(?:\.\d{1,3})?|1(?:\.0{1,3})?)$")


class AcceptLanguageError(ValueError):
    """Raised when an Accept-Language header value is syntactically invalid."""


class ParticipantHeaderError(AcceptLanguageError):
    """A group participant's Accept-Language header failed to parse.

    Carries the 1-based ``position`` and ``participant_id`` of the
    offending participant so the API layer can point the 422 response at
    the exact ``participants`` array element.
    """

    def __init__(self, participant_id: str, position: int, detail: str):
        self.participant_id = participant_id
        self.position = position
        super().__init__(
            f"第 {position} 位参与者 {participant_id!r} 的 "
            f"Accept-Language 非法: {detail}"
        )


class SampleHeaderError(AcceptLanguageError):
    """A coverage-plan sample's Accept-Language header failed to parse.

    Carries the 1-based ``position`` of the offending sample so the API
    layer can point the 422 response at the exact ``samples`` array
    element.
    """

    def __init__(self, position: int, detail: str):
        self.position = position
        super().__init__(
            f"第 {position} 个样本的 Accept-Language 非法: {detail}"
        )


class LocaleFallbackError(ValueError):
    """Raised when a ``locale_fallbacks`` mapping is structurally invalid."""


def normalize_fallbacks(
    raw: Any,
) -> dict[str, str]:
    """Validate the shape of a locale_fallbacks mapping and lowercase it.

    Keys/values must be well-formed BCP 47 tags (case-insensitive); keys
    may not repeat (case-insensitive). Raises
    :class:`LocaleFallbackError` otherwise.
    """

    if not isinstance(raw, dict):
        raise LocaleFallbackError("locale_fallbacks 必须是对象（源标签 -> 下一标签）")
    normalized: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not TAG_RE.fullmatch(key):
            raise LocaleFallbackError(f"不是合法的 BCP 47 源标签: {key!r}")
        if not isinstance(value, str) or not TAG_RE.fullmatch(value):
            raise LocaleFallbackError(
                f"源标签 {key!r} 的下一标签不是合法的 BCP 47 标签: {value!r}"
            )
        source = key.lower()
        if source in normalized:
            raise LocaleFallbackError(
                f"locale_fallbacks 存在重复源标签（大小写不敏感）: {key!r}"
            )
        normalized[source] = value.lower()
    return normalized


def validate_fallback_chains(
    chains: dict[str, str], supported: list[str]
) -> None:
    """Reject cycles and dangling chains that never reach a supported tag.

    Every source key must, by repeatedly following the directed edges,
    terminate at one of ``supported`` (case-insensitive). A loop on the
    way (including a self-loop before reaching a supported node) is a
    cycle; walking off the map without hitting a supported tag is a
    dangling chain.
    """

    supported_lower = {tag.lower() for tag in supported}
    for start in chains:
        seen: set[str] = set()
        node = start
        while True:
            if node in supported_lower:
                break
            if node in seen:
                raise LocaleFallbackError(
                    f"locale_fallbacks 从 {start!r} 出发存在环路"
                    f"（节点 {node!r} 被重复访问）"
                )
            seen.add(node)
            if node not in chains:
                raise LocaleFallbackError(
                    f"locale_fallbacks 从 {start!r} 出发的链在 {node!r} 处悬空："
                    "无法到达任何受支持语言"
                )
            node = chains[node]


@dataclass(frozen=True)
class LanguageRange:
    """A single ``language-range [ ";" "q=" qvalue ]`` entry."""

    range: str
    q: float
    position: int  # 1-based position in the header, used for diagnostics

    def to_dict(self) -> dict[str, Any]:
        return {"range": self.range, "q": self.q, "position": self.position}


def _parse_qvalue(raw: str, position: int) -> float:
    # OWS around ";" and "=" is allowed per RFC 9110, but the parameter
    # itself must be non-empty and of the form q=value.
    parameter = raw.strip()
    if not parameter:
        raise AcceptLanguageError(f"第 {position} 个语言范围含有空参数")
    if "=" not in parameter:
        raise AcceptLanguageError(
            f"第 {position} 个语言范围的 q 参数缺少取值"
        )
    key, _, value = parameter.partition("=")
    key, value = key.strip(), value.strip()
    if key != "q":
        raise AcceptLanguageError(
            f"第 {position} 个语言范围含有非法参数: {key!r}"
        )
    if not value:
        raise AcceptLanguageError(f"第 {position} 个语言范围的 q 参数缺少取值")
    if not _QVALUE_RE.match(value):
        raise AcceptLanguageError(
            f"第 {position} 个语言范围的 q 值非法: {value!r}"
        )
    return float(value)


def parse_accept_language(header: str | None) -> list[LanguageRange]:
    """Parse an Accept-Language header into ordered language ranges.

    Returns an empty list when the header is missing or whitespace-only.
    Duplicate ranges (same range, case-insensitive) keep their first
    occurrence; later occurrences are ignored.

    Raises :class:`AcceptLanguageError` on malformed syntax.
    """

    if header is None or not header.strip():
        return []

    ranges: list[LanguageRange] = []
    seen: set[str] = set()
    for position, element in enumerate(header.split(","), start=1):
        token = element.strip()
        if not token:
            # Trailing/empty element such as "en,," is a syntax error.
            raise AcceptLanguageError(f"第 {position} 个语言范围为空")

        # Split off parameters; OWS around ";" is permitted, so each
        # part is stripped. A space *inside* the tag token itself is not.
        parts = [part.strip() for part in element.split(";")]
        tag = parts[0]
        if not tag or " " in tag:
            raise AcceptLanguageError(
                f"第 {position} 个语言范围的标签非法: {tag!r}"
            )
        if not _RANGE_RE.match(tag):
            raise AcceptLanguageError(
                f"第 {position} 个语言范围不是合法的 BCP 47 标签: {tag!r}"
            )

        q = 1.0
        for parameter in parts[1:]:
            q = _parse_qvalue(parameter, position)

        normalized = tag.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        ranges.append(
            LanguageRange(range=normalized, q=q, position=position)
        )

    return ranges


def truncation_chain(tag: str) -> list[str]:
    """RFC 4647 section 3.4 lookup chain, always lowercase.

    Subtags are removed one at a time from the right; prefixes ending at
    or after a singleton subtag (extension markers like ``u`` or the
    privateuse marker ``x``) are skipped, because a singleton must never
    be the trailing subtag of a lookup prefix::

        zh-Hant-TW -> [zh-hant-tw, zh-hant, zh]
        zh-CN-u-ca-chinese -> [zh-cn-u-ca-chinese, zh-cn-u-ca, zh-cn, zh]
        en-US-x-foo -> [en-us-x-foo, en-us, en]
        x-foo-bar -> [x-foo-bar, x-foo]
    """

    subtags = tag.lower().split("-")
    chain: list[str] = []
    for end in range(len(subtags), 0, -1):
        last = subtags[end - 1]
        # Never stop on a singleton (extension / privateuse marker);
        # continue removing subtags from the right.
        if len(last) == 1:
            continue
        chain.append("-".join(subtags[:end]))
    return chain


def _find_concrete_range(
    chain: list[str], ranges: list[LanguageRange]
) -> tuple[LanguageRange | None, str | None]:
    """Return the first configured range matching any chain prefix.

    The chain is tried longest-first; within a prefix the first range in
    header order wins. Returns ``(range, matched_prefix)``.
    """

    by_range = {entry.range: entry for entry in ranges}
    for prefix in chain:
        entry = by_range.get(prefix)
        if entry is not None:
            return entry, prefix
    return None, None


def _wildcard_range(ranges: list[LanguageRange]) -> LanguageRange | None:
    for entry in ranges:
        if entry.range == "*":
            return entry
    return None


def _concrete_q_zero_excluded(
    tag_lower: str, zero_ranges: list[str]
) -> str | None:
    """Return a concrete q=0 range that excludes ``tag_lower``, if any.

    Exclusion uses the same lookup truncation as acceptance: the tag is
    excluded when one of its truncation prefixes is a concrete q=0 range.
    The wildcard ``*`` is deliberately not considered here; the
    fallbacks contract only forbids candidates excluded by concrete
    q=0 ranges.
    """

    excluded = set(zero_ranges)
    for prefix in truncation_chain(tag_lower):
        if prefix in excluded:
            return prefix
    return None


def _chain_fallback(
    parsed: list[LanguageRange],
    chains: dict[str, str],
    supported_lower_to_index: dict[str, int],
    zero_concrete: list[str],
) -> dict[str, Any] | None:
    """Walk locale_fallbacks chains from positive-q concrete ranges.

    Concrete ranges with strictly positive q are tried in descending q
    order, ties broken by header order. For each one whose label has an
    outgoing chain edge, the directed chain is followed: the first chain
    node that is a supported tag and is not excluded by a concrete q=0
    range wins. Returns a report dict, or ``None`` when no chain reaches
    an acceptable supported tag.
    """

    positive = [
        entry
        for entry in parsed
        if entry.range != "*" and entry.q > 0 and entry.range in chains
    ]
    positive.sort(key=lambda entry: (-entry.q, entry.position))

    for source in positive:
        path: list[str] = [source.range]
        visited: set[str] = {source.range}
        node = source.range
        while True:
            index = supported_lower_to_index.get(node)
            if index is not None:
                blocking = _concrete_q_zero_excluded(node, zero_concrete)
                if blocking is None:
                    return {
                        "source_range": source.range,
                        "source_q": source.q,
                        "source_position": source.position,
                        "path": path,
                        "selected_supported_index": index,
                    }
                # Supported but explicitly excluded: keep walking.
            nxt = chains.get(node)
            if nxt is None or nxt in visited:
                break
            visited.add(nxt)
            path.append(nxt)
            node = nxt
    return None


def _collect_ignored_duplicates(header: str | None) -> list[dict[str, Any]]:
    """Report duplicate header elements dropped by first-wins parsing.

    Purely diagnostic: mirrors the de-duplication done by
    :func:`parse_accept_language` and lists every later occurrence.
    """

    ignored: list[dict[str, Any]] = []
    if header and header.strip():
        seen: set[str] = set()
        for position, element in enumerate(header.split(","), start=1):
            token = element.strip()
            tag = token.split(";", 1)[0].strip().lower()
            if tag in seen:
                ignored.append({"range": tag, "position": position})
            else:
                seen.add(tag)
    return ignored


def _default_supported_index(supported: list[str], default: str) -> int:
    """Index of ``default`` inside ``supported`` (case-insensitive).

    The request schema guarantees the default is contained in the
    supported list case-insensitively, so the lookup must be too.
    """

    return next(
        i for i, tag in enumerate(supported) if tag.lower() == default.lower()
    )


def negotiate(
    header: str | None,
    supported: list[str],
    default: str,
    locale_fallbacks: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run the full negotiation and build the explanation payload.

    Parameters mirror the API request: ``header`` is the raw
    Accept-Language value, ``supported`` the server-supported BCP 47 tags
    in priority order, ``default`` the fallback tag (must be in
    ``supported``), ``locale_fallbacks`` an optional source-tag ->
    next-tag directed-chain mapping (already normalized to lowercase).
    """

    chains = (
        {source.lower(): target.lower() for source, target in locale_fallbacks.items()}
        if locale_fallbacks
        else {}
    )

    parsed = parse_accept_language(header)

    ignored_duplicates = _collect_ignored_duplicates(header)

    candidates: list[dict[str, Any]] = []
    for index, candidate in enumerate(supported):
        chain = truncation_chain(candidate)
        events: list[dict[str, Any]] = []

        matched, matched_prefix = _find_concrete_range(chain, parsed)
        for prefix in chain:
            if matched is not None and prefix == matched_prefix:
                events.append(
                    {
                        "truncated_to": prefix,
                        "outcome": "match",
                        "matched_range": matched.range,
                        "q": matched.q,
                        "header_position": matched.position,
                    }
                )
            else:
                events.append(
                    {"truncated_to": prefix, "outcome": "no-match"}
                )

        wildcard = _wildcard_range(parsed)
        if matched is None and wildcard is not None:
            events.append(
                {
                    "truncated_to": "*",
                    "outcome": "match",
                    "matched_range": "*",
                    "q": wildcard.q,
                    "header_position": wildcard.position,
                }
            )
        elif wildcard is not None:
            events.append(
                {
                    "truncated_to": "*",
                    "outcome": "skipped",
                    "detail": "存在更具体的语言范围匹配，通配符未参与",
                }
            )

        effective = matched if matched is not None else wildcard
        q_value = effective.q if effective is not None else None

        if not parsed:
            status = "no-range"
            detail = "请求头中没有有效语言范围"
        elif effective is None:
            status = "unmatched"
            detail = "所有逐级截断均未命中任何语言范围（含通配符）"
        elif effective.q == 0:
            status = "excluded"
            detail = f"匹配到的范围 {effective.range!r} q=0，候选被显式排除"
        else:
            status = "eligible"
            detail = "候选可接受"

        candidates.append(
            {
                "tag": candidate,
                "supported_index": index,
                "truncation_chain": chain,
                "events": events,
                "matched_range": (
                    effective.range if effective is not None else None
                ),
                "q": q_value,
                "status": status,
                "detail": detail,
            }
        )

    eligible = [c for c in candidates if c["status"] == "eligible"]
    selected_index: int | None = None
    chain_report: dict[str, Any] | None = None
    if eligible:
        # Highest client weight first; server list order breaks ties
        # (min() keeps the earliest supported_index among equal q).
        winner = min(eligible, key=lambda c: (-c["q"], c["supported_index"]))
        selected_index = winner["supported_index"]
        winner["status"] = "selected"
        winner["detail"] = "在所有可接受候选中权重最高（同权重按服务端列表顺序最早）"
    elif chains:
        # Standard lookup found nothing acceptable: try the directed
        # locale fallback chains, starting from the client's positive-q
        # concrete ranges in descending q / header order.
        zero_concrete = [
            entry.range
            for entry in parsed
            if entry.range != "*" and entry.q == 0
        ]
        supported_lower_to_index = {
            tag.lower(): index for index, tag in enumerate(supported)
        }
        chain_report = _chain_fallback(
            parsed, chains, supported_lower_to_index, zero_concrete
        )

    used_fallback = selected_index is None and chain_report is None
    if chain_report is not None:
        selected_index = chain_report["selected_supported_index"]
        winner = candidates[selected_index]
        winner["status"] = "selected"
        winner["detail"] = (
            f"标准 lookup 无可用候选，沿客户端范围 "
            f"{chain_report['source_range']!r} 的 locale_fallbacks 链 "
            f"{' -> '.join(chain_report['path'])} 找到该受支持标签"
        )
        message = (
            f"已按 locale_fallbacks 链从客户端范围 "
            f"{chain_report['source_range']!r} 沿 "
            f"{' -> '.join(chain_report['path'])} 选出语言"
        )
        reason = None
    elif used_fallback:
        selected_index = _default_supported_index(supported, default)
        if not parsed:
            reason = "no-valid-range"
            message = "Accept-Language 缺失或没有有效范围，回退到默认语言"
        elif not eligible:
            reason = "no-acceptable-candidate"
            message = "没有可接受的候选语言（均被 q=0 排除或未命中），回退到默认语言"
        else:  # pragma: no cover - defensive, eligible is non-empty above
            reason = "no-acceptable-candidate"
            message = "回退到默认语言"
        if chains and chain_report is None:
            message += "；locale_fallbacks 链也未找到未被 q=0 具体范围排除的受支持语言"
        default_report = candidates[selected_index]
        if default_report["status"] == "excluded":
            message += "；注意默认语言本身被客户端 q=0 显式排除"
    else:
        reason = None
        message = "已按客户端权重选出最佳语言"

    return {
        "selected_language": supported[selected_index],
        "selected_supported_index": selected_index,
        "fallback": used_fallback,
        "fallback_reason": reason,
        "message": message,
        "parsed_ranges": [entry.to_dict() for entry in parsed],
        "ignored_duplicates": ignored_duplicates,
        "candidates": candidates,
        "locale_fallback": (
            {
                "triggered": True,
                "source_range": chain_report["source_range"],
                "source_q": chain_report["source_q"],
                "source_position": chain_report["source_position"],
                "path": chain_report["path"],
                "selected_language": supported[
                    chain_report["selected_supported_index"]
                ],
            }
            if chain_report is not None
            else None
        ),
    }


def negotiate_group(
    participants: list[tuple[str, str | None]],
    supported: list[str],
    default: str,
) -> dict[str, Any]:
    """Negotiate a single language acceptable to a whole group.

    ``participants`` is a non-empty list of ``(id, accept_language)``
    pairs (id uniqueness is enforced by the request schema). Every
    participant's header is evaluated with the exact RFC 9110/4647
    lookup semantics of :func:`negotiate`, producing a
    participants x candidates weight matrix:

    * a candidate matched by any participant's *concrete* ``q=0`` range
      is globally vetoed (the wildcard ``*;q=0`` only zeroes that
      participant's weight, it does not veto);
    * a participant without a header (``None``/blank) abstains: every
      candidate counts as q=1 for them and they never veto (per
      RFC 9110 an absent Accept-Language means any language is
      acceptable);
    * among the surviving candidates the winner has the highest minimum
      personal weight, then the highest total weight, then the earliest
      position in ``supported``;
    * when no candidate is acceptable to everyone, ``default`` is
      returned with ``fallback`` set and reason
      ``no-common-acceptable-candidate``.
    """

    parsed_headers: list[list[LanguageRange]] = []
    participant_reports: list[dict[str, Any]] = []
    for position, (participant_id, header) in enumerate(participants, start=1):
        try:
            parsed = parse_accept_language(header)
        except AcceptLanguageError as exc:
            raise ParticipantHeaderError(
                participant_id, position, str(exc)
            ) from exc
        parsed_headers.append(parsed)
        participant_reports.append(
            {
                "id": participant_id,
                "position": position,
                "abstained": not parsed,
                "parsed_ranges": [entry.to_dict() for entry in parsed],
                "ignored_duplicates": _collect_ignored_duplicates(header),
            }
        )

    candidates: list[dict[str, Any]] = []
    for index, candidate in enumerate(supported):
        chain = truncation_chain(candidate)
        weights: list[dict[str, Any]] = []
        vetoed_by: list[str] = []
        for report, parsed in zip(participant_reports, parsed_headers):
            participant_id = report["id"]
            if report["abstained"]:
                weights.append(
                    {
                        "participant_id": participant_id,
                        "q": 1.0,
                        "matched_range": None,
                        "veto": False,
                    }
                )
                continue
            matched, _prefix = _find_concrete_range(chain, parsed)
            effective = matched if matched is not None else _wildcard_range(parsed)
            veto = matched is not None and matched.q == 0
            if veto:
                vetoed_by.append(participant_id)
            weights.append(
                {
                    "participant_id": participant_id,
                    "q": effective.q if effective is not None else 0.0,
                    "matched_range": (
                        effective.range if effective is not None else None
                    ),
                    "veto": veto,
                }
            )
        min_q = min(entry["q"] for entry in weights)
        # Sums of 3-decimal q values are exact 3-decimal decimals; the
        # rounding only absorbs binary float noise so that equal totals
        # tie reliably and fall through to the server-order tiebreak.
        total_q = round(sum(entry["q"] for entry in weights), 6)
        candidates.append(
            {
                "tag": candidate,
                "supported_index": index,
                "min_q": min_q,
                "total_q": total_q,
                "vetoed": bool(vetoed_by),
                "vetoed_by": vetoed_by,
                "status": (
                    "vetoed"
                    if vetoed_by
                    else "eligible" if min_q > 0 else "unacceptable"
                ),
                "weights": weights,
            }
        )

    eligible = [c for c in candidates if not c["vetoed"] and c["min_q"] > 0]
    if eligible:
        winner = min(
            eligible,
            key=lambda c: (-c["min_q"], -c["total_q"], c["supported_index"]),
        )
        winner["status"] = "selected"
        selected_index = winner["supported_index"]
        fallback = False
        reason = None
        message = (
            "已按群体规则选出共同接受的最佳语言"
            "（先比较最低个人权重，再比较总权重，最后按服务端列表顺序）"
        )
    else:
        selected_index = _default_supported_index(supported, default)
        fallback = True
        reason = "no-common-acceptable-candidate"
        message = (
            "没有任何候选被所有参与者共同接受"
            "（被具体范围 q=0 否决，或对某位参与者权重为 0），回退到默认语言"
        )
        if candidates[selected_index]["vetoed"]:
            message += "；注意默认语言本身被部分参与者以具体范围 q=0 否决"

    return {
        "selected_language": supported[selected_index],
        "selected_supported_index": selected_index,
        "fallback": fallback,
        "fallback_reason": reason,
        "message": message,
        "participants": participant_reports,
        "candidates": candidates,
    }


def plan_coverage(
    samples: list[tuple[str | None, float]],
    supported: list[str],
    default: str,
    candidates: list[str],
    max_additions: int,
) -> dict[str, Any]:
    """Plan which candidate languages to add for better traffic coverage.

    ``samples`` is a non-empty list of ``(accept_language, traffic)``
    pairs with non-negative traffic. Every sample is evaluated with the
    exact semantics of :func:`negotiate` (no ``locale_fallbacks``): first
    against the current ``supported`` list (the baseline), then against
    ``supported`` extended by each candidate subset of up to
    ``max_additions`` new languages (appended in candidate input order,
    so existing entries keep their tie-break priority).

    A sample counts as *improved* when it falls back to the default
    language in the baseline but is a normal hit after the additions;
    ``improved_traffic`` is the sum of improved samples' traffic. The
    exhaustive search ranks subsets by improved traffic, breaking ties
    by fewer additions and then by earlier candidate input order. The
    empty subset participates, so when no combination improves anything
    the plan recommends adding nothing.
    """

    baseline: list[tuple[str, bool]] = []
    for position, (header, _traffic) in enumerate(samples, start=1):
        try:
            outcome = negotiate(
                header=header, supported=supported, default=default
            )
        except AcceptLanguageError as exc:
            raise SampleHeaderError(position, str(exc)) from exc
        baseline.append((outcome["selected_language"], outcome["fallback"]))

    traffics = [float(traffic) for _header, traffic in samples]
    # Traffic sums absorb binary float noise via round(..., 6), mirroring
    # the group endpoint's total_q handling, so equal totals tie reliably.
    baseline_fallback_traffic = round(
        sum(
            traffic
            for traffic, (_selected, fell_back) in zip(traffics, baseline)
            if fell_back
        ),
        6,
    )
    fallback_indices = [
        index for index, (_selected, fell_back) in enumerate(baseline) if fell_back
    ]

    best_combo: tuple[int, ...] = ()
    best_improved = 0.0
    # Sizes ascend (fewer additions win ties) and combinations() yields
    # index tuples in candidate input order (earlier input wins ties), so
    # replacing only on a strictly greater improvement implements the
    # full tie-break contract.
    for size in range(1, max_additions + 1):
        for combo in combinations(range(len(candidates)), size):
            extended = supported + [candidates[i] for i in combo]
            improved = 0.0
            # Only baseline-fallback samples can convert to a normal hit.
            for index in fallback_indices:
                outcome = negotiate(
                    header=samples[index][0], supported=extended, default=default
                )
                if not outcome["fallback"]:
                    improved += traffics[index]
            improved = round(improved, 6)
            if improved > best_improved:
                best_improved = improved
                best_combo = combo

    additions = [candidates[i] for i in best_combo]
    extended = supported + additions
    sample_reports: list[dict[str, Any]] = []
    for position, ((header, traffic), (base_selected, base_fell_back)) in enumerate(
        zip(samples, baseline), start=1
    ):
        planned = negotiate(header=header, supported=extended, default=default)
        sample_reports.append(
            {
                "position": position,
                "traffic": traffic,
                "baseline_selected_language": base_selected,
                "baseline_fallback": base_fell_back,
                "planned_selected_language": planned["selected_language"],
                "planned_fallback": planned["fallback"],
                "improved": base_fell_back and not planned["fallback"],
            }
        )

    if additions:
        message = (
            f"穷举所有不超过 {max_additions} 个新增的候选组合后，新增 "
            f"{'、'.join(additions)} 可将 {best_improved:g} 的默认兜底流量"
            "转为正常命中（同分按新增数更少、候选输入序更早取舍）"
        )
    else:
        message = (
            f"所有不超过 {max_additions} 个新增的候选组合都无法将默认兜底流量"
            "转为正常命中，建议不新增语言"
        )

    return {
        "selected_additions": additions,
        "baseline_fallback_traffic": baseline_fallback_traffic,
        "improved_traffic": best_improved,
        "message": message,
        "samples": sample_reports,
    }


def analyze_q_stability(
    header: str | None,
    supported: list[str],
    default: str,
    locale_fallbacks: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Analyze how far each range's q may move without changing the outcome.

    Runs :func:`negotiate` once for the baseline outcome, then for every
    de-duplicated parsed range finds the maximal contiguous run of the
    RFC 9110 three-decimal q grid (``0.000``..``1.000``) that contains
    the range's original q and yields the same ``selected_language``
    *and* ``fallback`` flag as the baseline.

    Match resolution (concrete range vs wildcard vs nothing, truncation
    prefixes, q=0 exclusion) depends only on the *tags*, never on q
    values, so it is precomputed once per candidate; while scanning one
    range's q, every other range keeps its parsed q. A grid step is then
    evaluated by a lightweight mirror of :func:`negotiate`'s selection
    logic (winner by q then server order, then the locale_fallbacks
    chain, then the default fallback) that only reads the precomputed
    matches -- no header is re-parsed. Moreover, as the scanned q moves,
    the (selected_language, fallback) pair can change only when the
    scanned weight crosses 0 or another range's weight (ties resolved at
    equality), i.e. at a small set of event steps; the outcome is
    constant between them, so at most O(number of ranges) grid points
    per range need evaluating. Wildcards, truncation matching and q=0
    exclusion are therefore the exact same rules; a missing/blank
    header contains no ranges, so its stability report is empty.
    """

    baseline = negotiate(
        header=header,
        supported=supported,
        default=default,
        locale_fallbacks=locale_fallbacks,
    )
    parsed = parse_accept_language(header)
    baseline_key = (baseline["selected_language"], baseline["fallback"])

    chains = (
        {source.lower(): target.lower() for source, target in locale_fallbacks.items()}
        if locale_fallbacks
        else {}
    )
    supported_lower_to_index = {
        tag.lower(): index for index, tag in enumerate(supported)
    }
    default_index = _default_supported_index(supported, default)

    # Per candidate, the effective range depends solely on the tags:
    # (matched concrete entry or None, wildcard entry or None). Same
    # precedence as negotiate(): concrete longest-prefix match beats *.
    candidate_effective: list[tuple[LanguageRange | None, LanguageRange | None]] = []
    for candidate in supported:
        chain = truncation_chain(candidate)
        matched, _prefix = _find_concrete_range(chain, parsed)
        wildcard = _wildcard_range(parsed) if matched is None else None
        candidate_effective.append((matched, wildcard))

    def outcome(
        ranges: list[LanguageRange],
    ) -> tuple[str, bool]:
        """Mirror of negotiate()'s winner/fallback decision on ranges."""

        q_by_range = {entry.range: entry.q for entry in ranges}
        eligible: list[tuple[float, int]] = []
        for index, (matched, wildcard) in enumerate(candidate_effective):
            if matched is not None:
                q_value = q_by_range[matched.range]
            elif wildcard is not None:
                q_value = q_by_range["*"]
            else:
                continue
            if q_value > 0:
                eligible.append((q_value, index))
            # q == 0 -> excluded, exactly like negotiate(): wildcard and
            # concrete exclusions both keep the candidate out of the set.

        selected_index: int | None = None
        if eligible:
            _q, selected_index = min(
                eligible, key=lambda item: (-item[0], item[1])
            )
        elif chains:
            zero_concrete = {
                entry.range
                for entry in ranges
                if entry.range != "*" and entry.q == 0
            }
            positive = [
                entry
                for entry in ranges
                if entry.range != "*" and entry.q > 0 and entry.range in chains
            ]
            positive.sort(key=lambda entry: (-entry.q, entry.position))
            for source in positive:
                visited: set[str] = {source.range}
                node = source.range
                while True:
                    index = supported_lower_to_index.get(node)
                    if index is not None:
                        if (
                            _concrete_q_zero_excluded(node, list(zero_concrete))
                            is None
                        ):
                            selected_index = index
                            break
                        # Supported but explicitly excluded: keep walking,
                        # mirroring _chain_fallback.
                    nxt = chains.get(node)
                    if nxt is None or nxt in visited:
                        break
                    visited.add(nxt)
                    node = nxt
                if selected_index is not None:
                    break

        if selected_index is None:
            return supported[default_index], True
        return supported[selected_index], False

    def outcome_with_q(target: LanguageRange, q: float) -> tuple[str, bool]:
        varied = LanguageRange(
            range=target.range, q=q, position=target.position
        )
        ranges = [varied if entry is target else entry for entry in parsed]
        return outcome(ranges)

    # For a scanned range R with grid step s, every candidate's
    # effective weight is either s (the candidate's effective range is
    # R itself) or a constant taken from another range; eligibility also
    # changes at s=0. The selected_language/fallback pair can therefore
    # change only at step 0/1 and at grid steps adjacent to another
    # range's weight crossing (s == q_other +/- 1, and s == q_other for
    # ties). Between consecutive such event steps the outcome is
    # constant, so the stable run is found by classifying the spans
    # between sorted event steps instead of walking all 1001 points.
    other_steps = sorted(
        {round(other.q * Q_GRID_STEPS) for other in parsed}
    )

    range_reports: list[dict[str, Any]] = []
    for entry in parsed:
        original_step = round(entry.q * Q_GRID_STEPS)

        event_steps = {0, 1, Q_GRID_STEPS, original_step}
        for step in other_steps:
            event_steps.add(step)
            if step - 1 >= 0:
                event_steps.add(step - 1)
            if step + 1 <= Q_GRID_STEPS:
                event_steps.add(step + 1)
        ordered = sorted(event_steps)

        def step_outcome(step: int) -> tuple[str, bool]:
            return outcome_with_q(entry, step / Q_GRID_STEPS)

        # Span k covers [ordered[k], ordered[k+1]) (last one ends at
        # 1000 inclusive); its outcome is constant and equals the
        # outcome evaluated at its first step.
        starts = ordered
        ends_exclusive = ordered[1:] + [Q_GRID_STEPS + 1]
        span_stable = [
            step_outcome(step) == baseline_key for step in starts
        ]

        origin_span = next(
            k
            for k, start in enumerate(starts)
            if start <= original_step < ends_exclusive[k]
        )
        # The baseline was produced with the original q, so this span
        # necessarily reproduces it.
        assert span_stable[origin_span]

        first_span = origin_span
        while first_span > 0 and span_stable[first_span - 1]:
            first_span -= 1
        last_span = origin_span
        while last_span + 1 < len(starts) and span_stable[last_span + 1]:
            last_span += 1

        lower = starts[first_span]
        upper = ends_exclusive[last_span] - 1

        def outside_point(step: int) -> dict[str, Any] | None:
            if step < 0 or step > Q_GRID_STEPS:
                return None
            selected, fell_back = step_outcome(step)
            return {
                "q": step / Q_GRID_STEPS,
                "selected_language": selected,
                "fallback": fell_back,
            }

        range_reports.append(
            {
                "range": entry.range,
                "position": entry.position,
                "original_q": entry.q,
                "lower_bound": lower / Q_GRID_STEPS,
                "upper_bound": upper / Q_GRID_STEPS,
                "lower_outside": outside_point(lower - 1),
                "upper_outside": outside_point(upper + 1),
            }
        )

    return {"baseline": baseline, "ranges": range_reports}
