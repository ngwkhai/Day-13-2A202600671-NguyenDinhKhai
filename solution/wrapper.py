"""Mitigation + observability layer for the opaque ecommerce agent."""
from __future__ import annotations

import copy
import contextlib
import json
import os
import re
import time
import unicodedata

try:
    from telemetry.cost import cost_from_usage
    from telemetry.logger import logger, new_correlation_id, set_correlation_id
    from telemetry.redact import redact
except Exception:
    logger = None

    def cost_from_usage(model, usage):
        return 0.0

    def new_correlation_id():
        return "req-local"

    def set_correlation_id(cid):
        return None

    def redact(text):
        return text, 0


_PROMPT_CACHE = None
_NOTE_RE = re.compile(
    r"(?is)\b(?:ghi\s*ch[uú]|order\s*notes?|notes?)\b\s*[:：-].*(?=$|\n)"
)
_CONTROL_LINE_RE = re.compile(
    r"(?im)^\s*(?:system|developer|assistant)\s*[:：-].*$"
)
_INJECTION_PHRASE_RE = re.compile(
    r"(?is)(ignore\s+(?:all|previous|above).{0,120}|"
    r"kh[oô]ng\s+c[aầ]n\s+d[uù]ng\s+tool.{0,120}|"
    r"gia\s+(?:he\s+thong|moi|fake)\s*[:=].{0,80}|"
    r"gi[aá]\s+(?:h[eệ]\s+th[oố]ng|m[oớ]i|fake)\s*[:=].{0,80})"
)
_PRODUCT_CANON = [
    ("iphone", "iPhone"),
    ("macbook", "MacBook"),
    ("ipad", "iPad"),
    ("airpods", "AirPods"),
    ("samsung", "Samsung"),
    ("sony", "Sony"),
    ("oppo", "oppo"),
    ("xiaomi", "xiaomi"),
]
_DEST_CANON = [
    ("tp hcm", "TP HCM"),
    ("ho chi minh", "TP HCM"),
    ("hcm", "TP HCM"),
    ("ha noi", "Ha Noi"),
    ("da nang", "Da Nang"),
    ("hai phong", "Hai Phong"),
    ("can tho", "Can Tho"),
    ("da lat", "Da Lat"),
    ("vung tau", "Vung Tau"),
]
_PRODUCT_MARKER_RE = re.compile(
    r"\b(?:ap|áp)\s+d(?:u|ụ)ng\s+m(?:a|ã)\b|"
    r"\b(?:dung|dùng)\s+m(?:a|ã)\b|"
    r"\b(?:voi|với)\s+coupon\b|"
    r"\bcoupon\b|"
    r"\b(?:ship|giao)\b|"
    r"\b(?:tong|tổng|het|hết|bao)\b|"
    r"[,?.-]",
    re.I,
)


def _prompt_text():
    global _PROMPT_CACHE
    if _PROMPT_CACHE is not None:
        return _PROMPT_CACHE
    path = os.path.join(os.path.dirname(__file__), "prompt.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            _PROMPT_CACHE = fh.read().strip()
    except Exception:
        _PROMPT_CACHE = ""
    return _PROMPT_CACHE


def _sanitize_question(question):
    text = unicodedata.normalize("NFC", str(question or ""))
    text = _CONTROL_LINE_RE.sub(" ", text)
    text = _NOTE_RE.sub(" [order note removed] ", text)
    text = _INJECTION_PHRASE_RE.sub(" [instruction removed] ", text)
    return re.sub(r"\s+", " ", text).strip()


def _fold(text):
    text = unicodedata.normalize("NFD", str(text or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = text.replace("đ", "d").replace("Đ", "D")
    return text.lower()


def _canonical_product(product):
    folded = _fold(product)
    for needle, canon in _PRODUCT_CANON:
        if needle in folded:
            return canon
    return str(product or "").strip(" .,-") or None


def _canonical_destination(destination):
    folded = _fold(destination)
    for needle, canon in _DEST_CANON:
        if needle in folded:
            return canon
    return str(destination or "").strip(" .,-") or None


def _extract_fields(question):
    text = str(question or "")
    folded = _fold(text)
    qty = 1
    qty_match = re.search(r"\b(?:mua|dat|lay|order)\s+(\d+)\b", folded)
    if qty_match:
        qty = max(1, int(qty_match.group(1)))

    coupon = None
    coupon_match = re.search(
        r"\b(?:ap dung ma|dung ma|voi coupon|coupon)\s+([a-z0-9]+)",
        folded,
    )
    if coupon_match:
        coupon = coupon_match.group(1).upper()

    destination = None
    dest_match = re.search(
        r"\b(?:ship|giao)(?:\s+(?:den|đến))?\s+(.+?)(?=\s+(?:tong|tổng|het|hết|bao|voi|với|dung|dùng|ap|áp|coupon)|[,?.-]|$)",
        text,
        re.I,
    )
    if dest_match:
        destination = _canonical_destination(dest_match.group(1))

    product = None
    product_match = re.search(r"\b(?:mua|dat|đặt|lay|lấy|order)\s+(?:\d+\s+)?(.+)", text, re.I)
    if product_match:
        product = _PRODUCT_MARKER_RE.split(product_match.group(1), 1)[0].strip(" .,-")
    if not product:
        stock_match = re.search(r"\bcon\s+(.+?)\s+(?:khong|không)\b", text, re.I)
        if stock_match:
            product = stock_match.group(1).strip(" .,-")
    if not product:
        known_match = re.search(
            r"\b(iPhone|MacBook|iPad|AirPods|Samsung|Sony|Oppo|Xiaomi)\b",
            text,
            re.I,
        )
        if known_match:
            product = known_match.group(1)
    product = _canonical_product(product)

    want_total = any(term in folded for term in ("tong", "bao nhieu", "het bao nhieu", "ship", "giao"))
    return {
        "product": product,
        "qty": qty,
        "coupon": coupon,
        "destination": destination,
        "want_total": want_total,
    }


def _question_with_hints(question, fields):
    hints = []
    if fields.get("product"):
        hints.append(f"product={fields['product']}")
    hints.append(f"qty={fields.get('qty', 1)}")
    if fields.get("coupon"):
        hints.append(f"coupon={fields['coupon']}")
    if fields.get("destination"):
        hints.append(f"destination={fields['destination']}")
    if len(hints) <= 1:
        return question
    return f"{question}\nWrapper parsed fields for tool arguments: {'; '.join(hints)}"


def _lock_context(lock):
    return lock if lock is not None else contextlib.nullcontext()


def _cache_get(context, key):
    cache = context.get("cache")
    if not isinstance(cache, dict):
        return None
    with _lock_context(context.get("cache_lock")):
        value = cache.get(key)
        return copy.deepcopy(value) if value is not None else None


def _cache_put(context, key, result):
    cache = context.get("cache")
    if not isinstance(cache, dict):
        return
    with _lock_context(context.get("cache_lock")):
        cache[key] = copy.deepcopy(result)


def _trace_summary(trace):
    actions = []
    repeated_actions = 0
    tool_errors = 0
    seen = {}
    for item in trace or []:
        if isinstance(item, dict):
            action = item.get("action") or item.get("tool") or item.get("name")
            args = item.get("args") or item.get("input") or item.get("arguments")
            err = item.get("error")
            text = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str).lower()
        else:
            action = str(item)
            args = None
            err = None
            text = action.lower()
        if action:
            actions.append(str(action))
            sig = (str(action), json.dumps(args, sort_keys=True, default=str))
            seen[sig] = seen.get(sig, 0) + 1
            if seen[sig] == 2:
                repeated_actions += 1
        if err or "error" in text or "failed" in text or "exception" in text:
            tool_errors += 1
    return {
        "actions": actions,
        "tool_errors": tool_errors,
        "repeated_actions": repeated_actions,
    }


def _log(event, data):
    if logger:
        logger.log_event(event, data)


def _is_retryable(result, summary):
    if result is None:
        return True
    status = result.get("status")
    if status in {"loop", "max_steps", "no_action", "wrapper_error"}:
        return True
    if not result.get("answer"):
        return True
    return False


def _tool_observations(trace):
    observations = {}
    for item in trace or []:
        if not isinstance(item, dict):
            continue
        tool = item.get("tool") or item.get("name")
        obs = item.get("observation")
        if tool and isinstance(obs, dict):
            observations[tool] = obs
    return observations


def _fmt_int(value):
    return str(int(value))


def _answer_from_trace(trace, fields, original_answer):
    observations = _tool_observations(trace)
    stock = observations.get("check_stock")
    if not isinstance(stock, dict):
        return None

    product = fields.get("product") or stock.get("item") or "san pham"
    qty = int(fields.get("qty") or 1)

    if not stock.get("found", False):
        return f"Khong tim thay {product}. (no total)"
    if not stock.get("in_stock", False) or int(stock.get("quantity") or 0) <= 0:
        return f"{product} hien het hang. (no total)"
    available = int(stock.get("quantity") or 0)
    if qty > available:
        return f"Chi con {available} {product}, khong du so luong {qty}. (no total)"

    discount = observations.get("get_discount")
    percent = 0
    if isinstance(discount, dict):
        percent = int(discount.get("percent") or 0) if discount.get("valid", False) else 0

    shipping = observations.get("calc_shipping")
    shipping_cost = 0
    if isinstance(shipping, dict):
        if shipping.get("error") or shipping.get("cost_vnd") is None:
            destination = fields.get("destination") or shipping.get("destination") or "dia diem nay"
            return f"Khong ho tro giao den {destination}. (no total)"
        shipping_cost = int(shipping.get("cost_vnd") or 0)

    unit_price = int(stock.get("unit_price_vnd") or 0)
    if unit_price <= 0:
        return None

    subtotal = unit_price * qty
    discounted = subtotal * (100 - percent) // 100
    total = discounted + shipping_cost

    has_total = isinstance(original_answer, str) and "tong cong" in _fold(original_answer)
    if shipping is not None or discount is not None or fields.get("want_total") or has_total:
        return f"Tong cong: {_fmt_int(total)} VND"
    return f"Con hang: {available}. Gia: {_fmt_int(unit_price)} VND"


def mitigate(call_next, question, config, context):
    cid = f"{context.get('qid', 'q')}-{context.get('session_id', 's')}-{context.get('turn_index', 0)}"
    set_correlation_id(cid or new_correlation_id())

    safe_question = _sanitize_question(question)
    fields = _extract_fields(safe_question)
    routed_question = _question_with_hints(safe_question, fields)
    conf = dict(config or {})
    prompt = _prompt_text()
    if prompt:
        conf["system_prompt"] = prompt
    conf["temperature"] = min(float(conf.get("temperature", 0.2) or 0.2), 0.2)
    conf["loop_guard"] = True
    conf["normalize_unicode"] = True
    conf["redact_pii"] = True

    cache_key = "observathon:v3:" + routed_question.lower()
    if (conf.get("cache") or {}).get("enabled", False):
        cached = _cache_get(context, cache_key)
        if cached is not None:
            cached.setdefault("meta", {})["cache_hit"] = True
            _log("OBSERVATHON_CACHE_HIT", {
                "qid": context.get("qid"),
                "session_id": context.get("session_id"),
                "turn_index": context.get("turn_index"),
            })
            return cached

    retry_conf = conf.get("retry") or {}
    attempts = 1
    if retry_conf.get("enabled", False):
        attempts = max(1, min(3, int(retry_conf.get("max_attempts", 2) or 2)))
    backoff_ms = max(0, int(retry_conf.get("backoff_ms", 0) or 0))

    last_result = None
    last_summary = {}
    for attempt in range(1, attempts + 1):
        started = time.time()
        try:
            result = call_next(routed_question, conf)
        except Exception as exc:
            result = {
                "answer": None,
                "status": "wrapper_error",
                "steps": 0,
                "trace": [{"error": str(exc)}],
                "meta": {
                    "latency_ms": 0,
                    "usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    },
                    "model": conf.get("model"),
                    "provider": conf.get("provider"),
                    "tools_used": [],
                },
            }
        wall_ms = int((time.time() - started) * 1000)
        trace = (result or {}).get("trace", [])
        fixed_answer = _answer_from_trace(trace, fields, (result or {}).get("answer"))
        if fixed_answer and fixed_answer != (result or {}).get("answer"):
            result = dict(result or {})
            result["answer"] = fixed_answer
            meta_for_fix = dict(result.get("meta") or {})
            meta_for_fix["postprocessed"] = True
            result["meta"] = meta_for_fix

        summary = _trace_summary((result or {}).get("trace", []))
        meta = (result or {}).get("meta", {})
        usage = meta.get("usage", {})
        answer = (result or {}).get("answer") or ""
        redacted_answer, pii_count = redact(answer)
        if redacted_answer != answer:
            result = dict(result or {})
            result["answer"] = redacted_answer

        _log("OBSERVATHON_CALL", {
            "qid": context.get("qid"),
            "session_id": context.get("session_id"),
            "turn_index": context.get("turn_index"),
            "attempt": attempt,
            "status": (result or {}).get("status"),
            "wall_ms": wall_ms,
            "reported_latency_ms": meta.get("latency_ms"),
            "usage": usage,
            "cost_usd": cost_from_usage(meta.get("model", ""), usage),
            "steps": (result or {}).get("steps"),
            "answer": (result or {}).get("answer"),
            "tools_used": meta.get("tools_used", []),
            "trace": (result or {}).get("trace", []),
            "trace_summary": summary,
            "pii_redactions": pii_count,
            "question_changed_by_sanitizer": safe_question != str(question or ""),
            "routed_question": routed_question,
            "parsed_fields": fields,
            "postprocessed": meta.get("postprocessed", False),
        })

        last_result = result
        last_summary = summary
        if not _is_retryable(result, summary):
            break
        if attempt < attempts and backoff_ms:
            time.sleep(backoff_ms / 1000.0)

    if last_result is None:
        last_result = {
            "answer": "Khong the tinh tong cong an toan tu du lieu hien co. (no total)",
            "status": "ok",
            "steps": 0,
            "trace": [],
            "meta": {"tools_used": []},
        }
    elif not last_result.get("answer"):
        last_result = dict(last_result)
        last_result["answer"] = "Khong the tinh tong cong an toan tu du lieu hien co. (no total)"
        last_result["status"] = "ok"

    if last_result.get("status") == "ok" and not last_summary.get("tool_errors"):
        _cache_put(context, cache_key, last_result)
    return last_result
