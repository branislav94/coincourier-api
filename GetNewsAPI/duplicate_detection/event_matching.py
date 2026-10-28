"""Lightweight deterministic extraction of event facts."""

from __future__ import annotations

import re
import html
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .identities import normalize_source_content


_ASSET_ALIASES = {
    "bitcoin": "asset:BTC",
    "btc": "asset:BTC",
    "ethereum": "asset:ETH",
    "ether": "asset:ETH",
    "eth": "asset:ETH",
    "solana": "asset:SOL",
    "sol": "asset:SOL",
    "xrp": "asset:XRP",
    "bnb": "asset:BNB",
    "tether": "asset:USDT",
    "usdt": "asset:USDT",
}
_ORGANIZATION_ALIASES = {
    "blackrock": "org:blackrock",
    "binance": "org:binance",
    "circle": "org:circle",
    "coinbase": "org:coinbase",
    "federal reserve": "org:federal-reserve",
    "fed": "org:federal-reserve",
    "fomc": "org:federal-reserve",
    "federal open market committee": "org:federal-reserve",
    "kraken": "org:kraken",
    "ripple": "org:ripple",
    "securities and exchange commission": "org:sec",
    "sec": "org:sec",
    "jpmorgan": "org:jpmorgan",
    "j.p. morgan": "org:jpmorgan",
    "jp morgan": "org:jpmorgan",
    "j.p.morgan": "org:jpmorgan",
    "ethereum foundation": "org:ethereum-foundation",
    "dekabank": "org:dekabank",
    "deka bank": "org:dekabank",
    "sparkassen": "org:sparkassen",
    "commerzbank": "org:commerzbank",
    "deutsche bank": "org:deutsche-bank",
    "hsbc": "org:hsbc",
}
_SUBJECT_ALIASES = {
    "glamsterdam": "product:glamsterdam",
    "hoodi": "product:hoodi",
    "alpenglow": "product:alpenglow",
    "bitcoin core": "product:bitcoin-core",
    "security policy": "product:security-policy",
    "tokenized money market": "product:tokenized-money-market",
    "tokenised money market": "product:tokenized-money-market",
    "chinese law": "jurisdiction:chinese-law",
    "uk court": "org:uk-court",
    "british court": "org:uk-court",
}
_ACTION_PATTERNS = {
    "acquire": r"\b(?:acquire[sd]?|acquisition)\b",
    "approve": r"\b(?:approve[sd]?|approval)\b",
    "file": r"\b(?:file[sd]?|filing)\b",
    "halt": r"\b(?:halt(?:ed|s|ing)?|suspend(?:ed|s|ing)?)\b",
    "launch": r"\b(?:launch(?:ed|es|ing)?|introduc(?:e[sd]?|ing|tion)|bring[sd]?|brought|add(?:ed|s|ing)?)\b",
    "reject": r"\b(?:reject(?:ed|s|ing|ion)?)\b",
    "report": r"\b(?:report(?:ed|s|ing)?)\b",
    "resume": r"\b(?:resume[sd]?|resumption)\b",
    "sue": r"\b(?:sue[sd]?|suing|lawsuit)\b",
    "postpone": r"\b(?:postpon(?:e[sd]?|ing|ement)|delay(?:ed|s|ing)?)\b",
    "activate": r"\b(?:activat(?:e[sd]?|ing|ion)|go(?:es)? live|went live|mainnet live)\b",
    "complete": r"\b(?:complet(?:e[sd]?|ing|ion)|implement(?:ed|s|ing|ation)?)\b",
    "update": r"\b(?:updat(?:e[sd]?|ing)|revis(?:e[sd]?|ing|ion))\b",
    "rule": r"\b(?:rul(?:e[sd]?|ing)|appl(?:y|ies|ied|ying))\b",
    "cancel": r"\b(?:cancel(?:led|ed|s|ling|ing|lation)?)\b",
    "resolve": r"\b(?:resolv(?:e[sd]?|ing)|resolut(?:ion)|recover(?:ed|s|y)|patch(?:ed|es|ing)?)\b",
    "exploit": r"\b(?:exploit(?:ed|s|ation)?|hack(?:ed|s|ing)?)\b",
    "overturn": r"\b(?:overturn(?:ed|s|ing)?|revers(?:e[sd]?|al)|appeal(?:ed|s|ing)?)\b",
    "uphold": r"\b(?:uphold(?:s|ing)?|upheld)\b",
    "outflow": r"\b(?:outflows?|withdrawals?)\b",
    "inflow": r"\b(?:inflows?|deposits?)\b",
    "settle": r"\b(?:settl(?:e[sd]?|ing|ement))\b",
    "price_rise": r"\b(?:ris(?:e[sd]?|ing)|rose|surge[sd]?|rall(?:y|ies|ied)|climb(?:ed|s|ing)?)\b",
    "price_fall": r"\b(?:fall(?:en|s|ing)?|fell|drop(?:ped|s|ping)?|plunge[sd]?|declin(?:e[sd]?|ing)|tumbl(?:e[sd]?|ing)|retreat(?:ed|s|ing)?)\b",
    "offline": r"\b(?:offline|outage|unavailable|inoperable|down(?:time)?)\b",
    "online": r"\b(?:online|operational|restored|restarted)\b",
    "release": r"\bclient release\b",
    "hold": r"\b(?:holds?|held|holding|maintain(?:ed|s|ing)?|kept|unchanged)\b",
    "rate_cut": r"\b(?:cut(?:s|ting)?|lower(?:ed|s|ing)?)\b",
    "rate_hike": r"\b(?:hike[sd]?|hik(?:ing)|rais(?:e[sd]?|ing))\b",
    "guidance": r"\b(?:guidance|forecasts?|projections?|dot plot|press conference|expect(?:s|ed)?|restrictive|outlook)\b",
}
_POLICY_FOLLOWUP_RE = re.compile(r"\b(?:new|revised|updated|later|subsequent)\b.{0,60}\b(?:guidance|forecast|projection|press conference)\b|\bpress conference\b.{0,60}\b(?:adds|reveals|guidance|forecast)\b")
_UNRESOLVED_POLICY_CLAIM_RE = re.compile(r"\b(?:expects?|restrictive|outlook|accelerat(?:e[sd]?|ing))\b")
_ACTIVATION_OUTCOME_RE = re.compile(r"\b(?:activat(?:ed|es|ing)|go(?:es)? live|went live|mainnet live)\b")
_NAMED_FUND_RE = re.compile(r"\b((?:[A-Z][A-Za-z0-9.-]*[ \t]+){1,5})Funds?\b")
_GENERIC_FUND_WORDS = frozenset("jpmorgan jp morgan tokenized tokenised money market fund funds launches launch launched adds add brings bring brought introduced introduces introduce the on solana".split())
_RATE_RANGE_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*%?\s*(?:[-\u2013\u2014]|to|and)\s*(\d+(?:\.\d+)?)\s*%")
_VOTE_PAIR_RE = re.compile(r"(?<![\w.])(\d{1,3})\s*(?:[-\u2013\u2014]|to)\s*(\d{1,3})(?!\w|\.\d)")
_UNCERTAIN_RE = re.compile(r"\b(?:may|might|could|reportedly|rumou?rs?|unconfirmed|alleged(?:ly)?|propos(?:e[sd]?|al)|plans? to|expected to)\b")
_NEGATION_RE = re.compile(r"\b(?:not|never|no longer|den(?:y|ies|ied)|without|fails? to|failed to|(?:is|are|was|were|has|have|had|do|does|did|ca|could|wo|would|should)n['\u2019]t|(?:has |have |had )?yet to)\b")
_WEEKDAY_RE = re.compile(r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b")
_RELATIVE_DATE_RE = re.compile(r"\b(?:tomorrow|yesterday|today|next (?:week|month|year)|later (?:this|next) (?:week|month|year))\b")
_VALUE_ROLE_RE = re.compile(r"\b(?:now|previously|formerly|from|to|versus|vs|compared|current|prior|before|after)\b")
_PARTICIPANT_SELECTION_RE = re.compile(r"\b(?:select(?:ed|s|ing|ions?)?|appoint(?:ed|s|ing|ments?)?|elect(?:ed|s|ing|ions?)?)\b")
_REFERENCE_RE = re.compile(r"\b(?:case|docket|filing|decision)\s*(?:no\.?|number|#)?\s*([a-z0-9]+(?:[-/:][a-z0-9]+)+)\b")
_PARTICIPANT_RE = re.compile(
    r"\b([A-Z][A-Za-z&.-]+(?:[ \t]+[A-Z][A-Za-z&.-]+){0,3})[ \t]+"
    r"(?:joins?|joined|partners?|partnered|invests?|invested|acquires?|acquired|"
    r"files?|filed|approves?|approved|launches?|launched|adds?|added|becomes?|became|dissents?|dissented|opposes?|opposed|expects?|says?|said)\b"
)
_PARTICIPANT_COMPLEMENT_RE = re.compile(
    r"\b(?i:with|alongside|by)[ \t]+([A-Z][A-Za-z&.-]+(?:[ \t]+[A-Z][A-Za-z&.-]+){0,3})[ \t]+(?i:as|acting as|joining)\b"
)
_ISO_DATE_RE = re.compile(r"\b(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}\b")
_MONTH_DATE_RE = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\s+\d{1,2}(?:,?\s+(?:19|20)\d{2})?\b"
)
_PERCENT_RE = re.compile(r"(?<!\w)[+-]?\d+(?:\.\d+)?\s*%")
_MONEY_RE = re.compile(
    r"(?<!\w)(?:[$\u20ac\u00a3]\s*\d[\d,]*(?:\.\d+)?(?:\s*(?:million|billion|trillion|thousand|bn|tn|m|b|k))?"
    r"|\d[\d,]*(?:\.\d+)?\s*(?:million|billion|trillion)\s*(?:dollars?|euros?|pounds?))\b"
)
_QUANTITY_RE = re.compile(
    r"\b\d[\d,]*(?:\.\d+)?\s*(?:btc|eth|tokens?|coins?|users?|accounts?|transactions?|milliseconds?|seconds?|minutes?|hours?|days?|ms|tps|validators?|blocks?|basis points?)\b"
)
_OTHER_NUMBER_RE = re.compile(r"(?<![\w.])[+-]?\d[\d,]*(?:\.\d+)?(?!\w|\.\d)")
_MONTH_NUMBERS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


@dataclass(frozen=True)
class EventSignature:
    kind: str
    primary_entity: str
    action: str
    event_dates: tuple[str, ...]
    anchors: tuple[str, ...]


@dataclass(frozen=True)
class EventFacts:
    entities: tuple[str, ...]
    dates: tuple[str, ...]
    numbers: tuple[str, ...]
    actions: tuple[str, ...]
    uncertainty: tuple[str, ...] = ()
    event_signatures: tuple[EventSignature, ...] = ()

    @property
    def key_entities(self) -> frozenset[str]:
        return frozenset(item for item in self.entities if not item.startswith("source:"))


def _normalized_text(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _normalize_date(value: Any) -> str:
    cleaned = _normalized_text(value).replace(",", "").replace("/", "-")
    iso_match = re.fullmatch(r"((?:19|20)\d{2})-(\d{1,2})-(\d{1,2})", cleaned)
    if iso_match:
        year, month, day = (int(part) for part in iso_match.groups())
        return f"{year:04d}-{month:02d}-{day:02d}"
    parts = cleaned.split()
    if len(parts) in {2, 3} and parts[0][:3] in _MONTH_NUMBERS:
        month = _MONTH_NUMBERS[parts[0][:3]]
        day = int(parts[1])
        if len(parts) == 3:
            return f"{int(parts[2]):04d}-{month:02d}-{day:02d}"
        return f"{month:02d}-{day:02d}"
    return cleaned


def _normalize_number(value: Any) -> str:
    cleaned = _normalized_text(value).replace(",", "")
    cleaned = re.sub(r"([$\u20ac\u00a3])\s+", r"\1", cleaned)
    cleaned = re.sub(r"\s+%", "%", cleaned)
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?%", cleaned):
        cleaned = f"{_decimal_text(cleaned[:-1])}%"
    return cleaned


def _decimal_text(value: str) -> str:
    return format(Decimal(value).normalize(), "f")


def _typed_policy_numbers(text: str, *, rate_policy: bool) -> tuple[set[str], str]:
    """Preserve range/vote roles instead of flattening them into value bags."""
    if not rate_policy:
        return set(), text
    facts: set[str] = set()

    def rate(match: re.Match[str]) -> str:
        lower, upper = match.groups()
        if Decimal(lower) > Decimal(upper):
            return match.group(0)
        facts.add(f"rate_range:{_decimal_text(lower)}%-{_decimal_text(upper)}%")
        return " "

    remainder = _RATE_RANGE_RE.sub(rate, text)

    def vote(match: re.Match[str]) -> str:
        context = remainder[max(0, match.start() - 35):match.end() + 35]
        if re.search(r"\b(?:votes?|voted|voting|ballot)\b", context):
            facts.add(f"vote:{int(match[1])}-{int(match[2])}")
            return " "
        return match.group(0)

    return facts, _VOTE_PAIR_RE.sub(vote, remainder)


def _event_signatures(text: str, entities: set[str], actions: set[str], dates: set[str], typed: set[str]) -> tuple[EventSignature, ...]:
    valid_dates = []
    for value in sorted(dates):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            try:
                date.fromisoformat(value)
                valid_dates.append(value)
            except ValueError:
                pass
    signatures: list[EventSignature] = []

    def add(kind: str, primary: str, action: str, anchors: set[str]) -> None:
        signatures.append(EventSignature(kind, primary, action, tuple(valid_dates), tuple(sorted(anchors))))

    if "org:federal-reserve" in entities and re.search(r"\b(?:rates?|federal funds|target range)\b", text):
        rate_actions = actions & {"hold", "rate_cut", "rate_hike"}
        if len(rate_actions) == 1:
            anchors = set(typed)
            if "meeting" in text and re.search(r"\b(?:fomc|federal open market committee)\b", text):
                anchors.add("meeting:fomc")
            add("fomc_rate_decision", "org:federal-reserve", next(iter(rate_actions)), anchors)
    if {"org:jpmorgan", "asset:SOL", "product:tokenized-money-market"} <= entities and "launch" in actions:
        names = {item for item in entities if item.startswith("named-fund:")}
        add("jpmorgan_solana_fund_launch", "org:jpmorgan", "launch", (names or {"product:tokenized-money-market"}) | {"target:solana"})
    if {"asset:ETH", "product:hoodi", "product:glamsterdam"} <= entities and "postpone" in actions and "testnet" in text and re.search(r"\b(?:fork|activation)\b", text) and "client release" not in text:
        add("ethereum_hoodi_postponement", "asset:ETH", "postpone", {"product:hoodi", "product:glamsterdam", "milestone:testnet_activation"})
    if {"asset:SOL", "product:alpenglow"} <= entities and "activate" in actions and "mainnet" in text:
        add("solana_alpenglow_activation", "asset:SOL", "activate", {"product:alpenglow", "milestone:mainnet_activation"})
    if {"product:bitcoin-core", "product:security-policy"} <= entities and "update" in actions and re.search(r"\bai\b|artificial intelligence", text) and re.search(r"\b(?:vulnerabilit\w*|bugs?|discovery)\b", text):
        aspects = {item for item in entities if item.startswith("policy-aspect:")}
        add("bitcoin_core_ai_security_policy", "product:bitcoin-core", "update", (aspects or {"target:security-policy"}) | {"context:ai_vulnerability_discovery"})
    return tuple(signatures)


@dataclass(frozen=True)
class CrossSourceEvidence:
    strong_identity: bool = False
    contradiction_flags: tuple[str, ...] = ()
    confirmation_anchors: tuple[str, ...] = ()
    plausible_identity: bool = False


def compare_cross_source_events(current: EventFacts, prior: EventFacts) -> CrossSourceEvidence:
    """Confirm only supported, date-specific signatures with two strong anchors."""
    contradictions: set[str] = set()
    for prefix, reason in (("rate_range:", "conflicting_rate_range"), ("vote:", "conflicting_vote")):
        left = {item for item in current.numbers if item.startswith(prefix)}
        right = {item for item in prior.numbers if item.startswith(prefix)}
        if left and right and left != right:
            contradictions.add(reason)
    if len(current.event_signatures) != 1 or len(prior.event_signatures) != 1:
        return CrossSourceEvidence(contradiction_flags=tuple(sorted(contradictions)))
    left, right = current.event_signatures[0], prior.event_signatures[0]
    if left.kind != right.kind or left.primary_entity != right.primary_entity:
        return CrossSourceEvidence(contradiction_flags=tuple(sorted(contradictions)))
    if left.action != right.action:
        contradictions.add("different_action_or_status")
    if left.event_dates and right.event_dates and left.event_dates != right.event_dates:
        contradictions.add("different_event_date")
    shared = tuple(sorted(set(left.anchors) & set(right.anchors)))
    for prefix in ("named-fund:", "policy-aspect:"):
        left_targets = {item for item in left.anchors if item.startswith(prefix)}
        right_targets = {item for item in right.anchors if item.startswith(prefix)}
        if left_targets and right_targets and left_targets != right_targets:
            contradictions.add("different_named_target")
    same_context = bool(left.action == right.action and len(left.event_dates) == 1 and left.event_dates == right.event_dates)
    strong = bool(same_context and len(shared) >= 2)
    return CrossSourceEvidence(strong, tuple(sorted(contradictions)), shared, bool(same_context and shared))


def _structured_values(value: Any) -> Iterable[str]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, (list, tuple, set, frozenset)):
        return (str(item).strip() for item in value if str(item).strip())
    return (str(value).strip(),)


def _matches_alias(text: str, alias: str) -> bool:
    pattern = r"(?:\s+|[-\u2013\u2014])".join(re.escape(part) for part in alias.split())
    return re.search(rf"(?<!\w){pattern}(?!\w)", text) is not None


def extract_event_facts(article: Mapping[str, Any]) -> EventFacts:
    title = _normalized_text(article.get("title"))
    source_content = normalize_source_content(article.get("text") or article.get("full_text"))
    combined = f"{title} {source_content}".strip()

    entities: set[str] = set()
    for ticker in _structured_values(article.get("tickers")):
        entities.add(f"asset:{ticker.upper()}")
    for alias, identity in _ASSET_ALIASES.items():
        if _matches_alias(combined, alias):
            entities.add(identity)
    for alias, identity in _ORGANIZATION_ALIASES.items():
        if _matches_alias(combined, alias):
            entities.add(identity)
    for alias, identity in _SUBJECT_ALIASES.items():
        if _matches_alias(combined, alias):
            entities.add(identity)
    if re.search(r"\bclient release\b", combined):
        entities.add("milestone:client_release")
    original_text = html.unescape(re.sub(r"<[^>]*>", " ", str(article.get("title") or "") + "\n" + str(article.get("text") or article.get("full_text") or "")))
    if "org:jpmorgan" in entities and "product:tokenized-money-market" in entities:
        for name in _NAMED_FUND_RE.findall(original_text):
            words = [word for word in _normalized_text(name).split() if word.replace(".", "") not in _GENERIC_FUND_WORDS]
            if words:
                entities.add("named-fund:" + " ".join(words))
    if "product:security-policy" in entities:
        for aspect in re.findall(r"\b([a-z]+) (?:rules?|requirements?)\b", combined):
            entities.add("policy-aspect:" + aspect)
    known_aliases = {**_ASSET_ALIASES, **_ORGANIZATION_ALIASES, **_SUBJECT_ALIASES}
    for participant in (*_PARTICIPANT_RE.findall(original_text), *_PARTICIPANT_COMPLEMENT_RE.findall(original_text)):
        normalized = _normalized_text(participant)
        if normalized not in known_aliases and not any(normalized.startswith(alias + " ") for alias in _ASSET_ALIASES):
            entities.add(f"actor:{normalized}")
    # Explicit structured participants supplement the small vocabulary; an
    # outlet name is deliberately not a participant or an identity namespace.
    structured = article.get("event_facts")
    structured = structured if isinstance(structured, Mapping) else {}
    for field in ("actors", "organizations", "products", "protocols"):
        for value in _structured_values(article.get(field) or structured.get(field)):
            normalized = _normalized_text(value)
            if normalized:
                entities.add(f"{field}:{normalized}")
    source_name = _normalized_text(article.get("source_name"))
    if source_name:
        entities.add(f"source:{source_name}")

    dates = {_normalize_date(match) for match in _ISO_DATE_RE.findall(combined)}
    dates.update(_normalize_date(match) for match in _MONTH_DATE_RE.findall(combined))
    dates.update(f"weekday:{match}" for match in _WEEKDAY_RE.findall(combined))

    rate_policy = "org:federal-reserve" in entities and re.search(r"\b(?:rates?|federal funds|target range)\b", combined) is not None
    typed_numbers, numeric_text = _typed_policy_numbers(combined, rate_policy=rate_policy)
    numbers = set(typed_numbers)
    numbers.update(_normalize_number(match) for match in _PERCENT_RE.findall(numeric_text))
    numbers.update(_normalize_number(match) for match in _MONEY_RE.findall(numeric_text))
    numbers.update(_normalize_number(match) for match in _QUANTITY_RE.findall(numeric_text))
    numbers.update(f"reference:{match}" for match in _REFERENCE_RE.findall(numeric_text))
    # Do not silently declare no numeric delta when a result uses an unfamiliar
    # unit. Remove typed facts first so equivalent date formats remain equal.
    untyped = numeric_text
    for pattern in (_ISO_DATE_RE, _MONTH_DATE_RE, _PERCENT_RE, _MONEY_RE, _QUANTITY_RE, _REFERENCE_RE):
        untyped = pattern.sub(" ", untyped)
    numbers.update(f"number:{_normalize_number(match)}" for match in _OTHER_NUMBER_RE.findall(untyped))

    actions = {
        action
        for action, pattern in _ACTION_PATTERNS.items()
        if re.search(pattern, combined)
    }
    if {"product:hoodi", "product:glamsterdam"} <= entities and "postpone" in actions and not _ACTIVATION_OUTCOME_RE.search(combined):
        actions.discard("activate")  # The delayed activation is a target, not a completed outcome.
    uncertainty = set()
    if _UNCERTAIN_RE.search(combined):
        uncertainty.add("uncertain_outcome_or_status")
    if _RELATIVE_DATE_RE.search(combined):
        uncertainty.add("unresolved_relative_date")
    untyped_roles = {item for item in numbers if not item.startswith(("rate_range:", "vote:"))}
    if len(untyped_roles) > 1 and _VALUE_ROLE_RE.search(numeric_text):
        # Sets retain values but lose which one is current, prior, or a target.
        uncertainty.add("unresolved_numeric_roles")
    if any(sum(item.startswith(prefix) for item in typed_numbers) > 1 for prefix in ("rate_range:", "vote:")):
        uncertainty.add("unresolved_numeric_roles")
    if len(dates) > 1 and _VALUE_ROLE_RE.search(combined):
        uncertainty.add("unresolved_date_roles")
    if _PARTICIPANT_SELECTION_RE.search(combined):
        # Participant roles selected/appointed/elected by another actor are not
        # represented by the entity/action sets. Never treat their absence as
        # proof that no new participant or outcome was reported.
        uncertainty.add("unresolved_participant_selection")
    if _POLICY_FOLLOWUP_RE.search(combined):
        uncertainty.add("new_policy_guidance_or_forecast")
    if rate_policy and _UNRESOLVED_POLICY_CLAIM_RE.search(combined):
        uncertainty.add("unresolved_policy_guidance")
    participants = {item for item in entities if item.startswith(("org:", "actor:", "actors:", "organizations:"))}
    if len(participants) > 1 and actions & {"sue", "acquire"}:
        # Without subject/object relations, reversing plaintiff/defendant or
        # buyer/target would otherwise look like the very same event facts.
        uncertainty.add("unresolved_participant_roles")
    if _NEGATION_RE.search(combined):
        # A bag of action words cannot reliably attach negation to its subject.
        # Keep such pairs observable and fail open rather than claim sameness.
        uncertainty.add("negated_outcome_or_status")
    for value in _structured_values(article.get("actions") or structured.get("actions")):
        actions.add(_normalized_text(value))
    signatures = _event_signatures(combined, entities, actions, dates, typed_numbers)
    if signatures and any(len(signature.event_dates) > 1 for signature in signatures):
        uncertainty.add("unresolved_multiple_event_dates")
    return EventFacts(
        entities=tuple(sorted(entities)),
        dates=tuple(sorted(dates)),
        numbers=tuple(sorted(numbers)),
        actions=tuple(sorted(actions)),
        uncertainty=tuple(sorted(uncertainty)),
        event_signatures=signatures,
    )
