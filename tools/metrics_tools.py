import logging
import math
from typing import Any, Dict, List, Optional, Tuple


logger = logging.getLogger(__name__)


class SessionMetricsMixin:
    """Provides shared helpers for Codex session metrics and rate-limit tracking."""

    def _estimate_tokens(self, *texts: Optional[str]) -> int:
        combined = " ".join(text for text in texts if text)
        if not combined:
            return 0
        estimated = math.ceil(len(combined) / 4)
        return max(int(estimated), 0)

    def _to_plain_dict(self, value: Any) -> Optional[Dict[str, Any]]:
        if value is None:
            return None
        if isinstance(value, dict):
            return value
        for converter in ("model_dump", "dict", "to_dict"):
            method = getattr(value, converter, None)
            if callable(method):
                try:
                    plain = method()
                    if isinstance(plain, dict):
                        return plain
                except Exception:
                    continue
        data = getattr(value, "__dict__", None)
        if isinstance(data, dict):
            simplified: Dict[str, Any] = {}
            for key, item in data.items():
                if key.startswith("_"):
                    continue
                if isinstance(item, (str, int, float, bool, dict, list, tuple, type(None))):
                    simplified[key] = item
            if simplified:
                return simplified
        return None

    def _merge_dicts(self, base: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(base)
        for key, value in updates.items():
            if (
                key in merged
                and isinstance(merged[key], dict)
                and isinstance(value, dict)
            ):
                merged[key] = self._merge_dicts(merged[key], value)
            else:
                merged[key] = value
        return merged

    def _flatten_numeric_entries(self, data: Any, prefix: str = "") -> List[Tuple[str, float]]:
        entries: List[Tuple[str, float]] = []
        if isinstance(data, dict):
            for key, value in data.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                entries.extend(self._flatten_numeric_entries(value, path))
        elif isinstance(data, (list, tuple)):
            for index, item in enumerate(data):
                path = f"{prefix}[{index}]" if prefix else f"[{index}]"
                entries.extend(self._flatten_numeric_entries(item, path))
        elif isinstance(data, (int, float)):
            entries.append((prefix, float(data)))
        return entries

    def _find_numeric_by_terms(
        self,
        entries: List[Tuple[str, float]],
        term_sets: List[Tuple[str, ...]],
    ) -> Optional[float]:
        for terms in term_sets:
            for path, value in entries:
                path_lower = path.lower()
                if all(term in path_lower for term in terms):
                    return value
        return None

    def _extract_usage_metrics(
        self,
        codex_result: Any,
        prompt: str,
        output_text: str,
    ) -> Dict[str, Any]:
        payloads: List[Dict[str, Any]] = []
        rate_limits_payload: Dict[str, Any] = {}

        for attr_name in ("metrics", "usage", "usage_metrics", "token_usage", "rate_limits", "metadata"):
            attr = getattr(codex_result, attr_name, None)
            plain = self._to_plain_dict(attr)
            if plain:
                payloads.append(plain)
                if attr_name == "rate_limits":
                    rate_limits_payload = self._merge_dicts(rate_limits_payload, plain)

        combined: Dict[str, Any] = {}
        for payload in payloads:
            combined = self._merge_dicts(combined, payload)

        flat_entries = self._flatten_numeric_entries(combined)

        total_tokens = self._find_numeric_by_terms(
            flat_entries,
            [
                ("total", "token"),
                ("token", "total"),
            ],
        )
        delta_tokens = self._find_numeric_by_terms(
            flat_entries,
            [
                ("delta", "token"),
                ("token", "delta"),
                ("tokens", "used"),
                ("used", "token"),
            ],
        )
        five_hour_used = self._find_numeric_by_terms(
            flat_entries,
            [
                ("five", "hour", "used"),
                ("5", "hour", "used"),
                ("five-hour", "used"),
            ],
        )
        five_hour_limit = self._find_numeric_by_terms(
            flat_entries,
            [
                ("five", "hour", "limit"),
                ("5", "hour", "limit"),
                ("five-hour", "limit"),
            ],
        )
        five_hour_remaining = self._find_numeric_by_terms(
            flat_entries,
            [
                ("five", "hour", "remaining"),
                ("5", "hour", "remaining"),
                ("five-hour", "remaining"),
            ],
        )
        weekly_used = self._find_numeric_by_terms(
            flat_entries,
            [
                ("week", "used"),
                ("weekly", "used"),
            ],
        )
        weekly_limit = self._find_numeric_by_terms(
            flat_entries,
            [
                ("week", "limit"),
                ("weekly", "limit"),
            ],
        )
        weekly_remaining = self._find_numeric_by_terms(
            flat_entries,
            [
                ("week", "remaining"),
                ("weekly", "remaining"),
            ],
        )

        metrics: Dict[str, Any] = {}
        if total_tokens is not None:
            metrics["total_tokens"] = int(total_tokens)
        if delta_tokens is not None:
            metrics["delta_tokens"] = int(delta_tokens)

        window_metrics: Dict[str, Dict[str, Any]] = {}
        if five_hour_used is not None or five_hour_limit is not None or five_hour_remaining is not None:
            window_metrics["five_hour"] = {
                "used": int(five_hour_used) if five_hour_used is not None else None,
                "limit": int(five_hour_limit) if five_hour_limit is not None else None,
                "remaining": int(five_hour_remaining) if five_hour_remaining is not None else None,
                "last_updated": self._current_time_iso(),
            }

        if weekly_used is not None or weekly_limit is not None or weekly_remaining is not None:
            window_metrics["weekly"] = {
                "used": int(weekly_used) if weekly_used is not None else None,
                "limit": int(weekly_limit) if weekly_limit is not None else None,
                "remaining": int(weekly_remaining) if weekly_remaining is not None else None,
                "last_updated": self._current_time_iso(),
            }

        if window_metrics:
            metrics.update(window_metrics)

        if rate_limits_payload:
            metrics["rate_limits"] = rate_limits_payload

        if combined:
            metrics["raw_snapshot"] = combined

        if "delta_tokens" not in metrics:
            metrics["delta_tokens"] = self._estimate_tokens(prompt, output_text)

        if "total_tokens" not in metrics:
            metrics["total_tokens"] = metrics["delta_tokens"]

        return metrics

    def _prepare_metrics_update(
        self,
        session_id: str,
        prompt: str,
        output_text: str,
        codex_result: Any,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        extracted = self._extract_usage_metrics(codex_result, prompt, output_text)
        existing_metrics = self.session_store.get_metrics(session_id) or {}
        token_usage_existing = existing_metrics.get("token_usage", {})

        delta_tokens = extracted.get("delta_tokens")
        if delta_tokens is None:
            delta_tokens = self._estimate_tokens(prompt, output_text)
        try:
            delta_tokens_int = max(int(delta_tokens), 0)
        except (TypeError, ValueError):
            delta_tokens_int = self._estimate_tokens(prompt, output_text)

        reported_total = extracted.get("total_tokens")
        try:
            reported_total_int = int(reported_total) if reported_total is not None else None
        except (TypeError, ValueError):
            reported_total_int = None

        previous_total = token_usage_existing.get("total_tokens")
        try:
            previous_total_int = int(previous_total) if previous_total is not None else 0
        except (TypeError, ValueError):
            previous_total_int = 0

        new_total_tokens = reported_total_int if reported_total_int is not None else previous_total_int + delta_tokens_int

        token_usage_update: Dict[str, Any] = {
            "total_tokens": new_total_tokens,
        }

        for window_key in ("five_hour", "weekly"):
            window_existing = token_usage_existing.get(window_key, {}) if isinstance(token_usage_existing, dict) else {}
            window_extracted = extracted.get(window_key, {})
            if not isinstance(window_existing, dict):
                window_existing = {}
            if not isinstance(window_extracted, dict):
                window_extracted = {}

            window_update: Dict[str, Any] = {}
            for field in ("used", "limit", "remaining", "last_updated"):
                field_value = window_extracted.get(field)
                if field_value is not None:
                    window_update[field] = field_value

            if "used" not in window_update and window_existing.get("used") is not None:
                try:
                    window_update["used"] = int(window_existing.get("used")) + delta_tokens_int
                except (TypeError, ValueError):
                    window_update["used"] = window_existing.get("used")

            if window_update:
                merged_window = dict(window_existing)
                merged_window.update(window_update)
                merged_window.setdefault("last_updated", self._current_time_iso())
                token_usage_update[window_key] = merged_window

        metrics_update: Dict[str, Any] = {
            "token_usage": token_usage_update,
            "last_task_tokens": delta_tokens_int,
        }

        rate_limits = extracted.get("rate_limits")
        if isinstance(rate_limits, dict) and rate_limits:
            metrics_update["rate_limits"] = rate_limits

        raw_snapshot = extracted.get("raw_snapshot")
        if isinstance(raw_snapshot, dict) and raw_snapshot:
            metrics_update["last_snapshot"] = raw_snapshot

        entry_extra: Dict[str, Any] = {
            "tokens_used": delta_tokens_int,
        }
        if rate_limits:
            entry_extra["rate_limits"] = rate_limits

        return metrics_update, entry_extra

    def _refresh_warning_cache(self, session_id: str) -> None:
        record = self._get_session_record(session_id)
        if not record:
            return
        warnings = (
            record.metadata.get("metrics", {})
            .get("token_usage", {})
            .get("warnings", {})
        )
        if isinstance(warnings, dict):
            self.rate_limit_warning_cache[session_id] = {
                "five_hour": list(warnings.get("five_hour", [])),
                "weekly": list(warnings.get("weekly", [])),
            }

    def _maybe_emit_usage_warnings(self, session_id: str) -> Optional[str]:
        record = self._get_session_record(session_id)
        if not record:
            return None
        token_usage = (
            record.metadata.get("metrics", {})
            .get("token_usage", {})
        )
        if not isinstance(token_usage, dict):
            return None

        warnings_cache = self.rate_limit_warning_cache.setdefault(
            session_id,
            {"five_hour": [], "weekly": []},
        )

        messages: List[str] = []
        for window_key, label in (("five_hour", "5-hour"), ("weekly", "weekly")):
            window_data = token_usage.get(window_key, {})
            if not isinstance(window_data, dict):
                continue
            limit = window_data.get("limit")
            used = window_data.get("used")

            try:
                limit_int = int(limit) if limit is not None else None
                used_int = int(used) if used is not None else None
            except (TypeError, ValueError):
                limit_int = None
                used_int = None

            if not limit_int or not used_int or limit_int <= 0:
                continue

            percent = (used_int / limit_int) * 100
            triggered_levels = warnings_cache.setdefault(window_key, [])

            for threshold in self.utilization_warning_thresholds:
                if percent >= threshold and threshold not in triggered_levels:
                    try:
                        self.session_store.record_usage_warning(session_id, window_key, threshold)
                    except Exception as error:
                        logger.exception(
                            "Failed to persist usage warning %s%% for %s: %s",
                            threshold,
                            window_key,
                            error,
                        )
                    triggered_levels.append(threshold)
                    messages.append(
                        f"Warning: Codex {label} token usage reached {percent:.1f}% "
                        f"({used_int} of {limit_int} tokens)."
                    )
                    break

        if messages:
            return " ".join(messages)
        return None

    def _format_usage_summary(self, metrics: Dict[str, Any]) -> str:
        token_usage = metrics.get("token_usage", {}) if isinstance(metrics, dict) else {}
        total_tokens = token_usage.get("total_tokens")
        summary_parts = []
        if total_tokens is not None:
            summary_parts.append(f"Total tokens used: {total_tokens}.")
        last_tokens = metrics.get("last_task_tokens")
        if last_tokens is not None:
            summary_parts.append(f"Last task consumed approximately {last_tokens} tokens.")

        for window_key, label in (("five_hour", "5-hour"), ("weekly", "weekly")):
            window = token_usage.get(window_key, {})
            if not isinstance(window, dict):
                continue
            limit = window.get("limit")
            used = window.get("used")
            remaining = window.get("remaining")
            if limit and used:
                try:
                    percent = (int(used) / int(limit)) * 100 if int(limit) else None
                except (TypeError, ValueError, ZeroDivisionError):
                    percent = None
                if percent is not None:
                    remaining_text = f", {int(remaining)} remaining" if remaining is not None else ""
                    summary_parts.append(
                        f"{label} window usage: {int(used)} of {int(limit)} tokens "
                        f"({percent:.1f}% used{remaining_text})."
                    )
            elif used:
                summary_parts.append(f"{label} window usage: {int(used)} tokens consumed.")

        if not summary_parts:
            return "I do not have token utilization metrics for this session yet."
        return " ".join(summary_parts)

    def _format_rate_limit_status(self, metrics: Dict[str, Any]) -> str:
        token_usage = metrics.get("token_usage", {}) if isinstance(metrics, dict) else {}
        lines = []
        for window_key, label in (("five_hour", "5-hour"), ("weekly", "weekly")):
            window = token_usage.get(window_key, {})
            if not isinstance(window, dict):
                lines.append(f"{label.capitalize()} utilization details are not available yet.")
                continue
            limit = window.get("limit")
            used = window.get("used")
            remaining = window.get("remaining")
            if limit and used:
                try:
                    percent = (int(used) / int(limit)) * 100 if int(limit) else None
                except (TypeError, ValueError, ZeroDivisionError):
                    percent = None
                if percent is not None:
                    remaining_text = f" with {int(remaining)} tokens remaining" if remaining is not None else ""
                    lines.append(
                        f"{label.capitalize()} window usage is at {percent:.1f}% "
                        f"({int(used)} of {int(limit)} tokens used{remaining_text})."
                    )
                    continue
            lines.append(f"{label.capitalize()} utilization details are not available yet.")
        if not lines:
            return "I do not have rate limit information for this session."
        return " ".join(lines)

    def _post_process_codex_activity(
        self,
        prompt: str,
        output_text: str,
        codex_result: Any,
        entry_type: str = "task",
    ) -> Optional[str]:
        session_id = getattr(self.CodexAgent.session, "session_id", None)
        if not session_id:
            return None

        try:
            metrics_update, entry_extra = self._prepare_metrics_update(session_id, prompt, output_text, codex_result)
        except Exception as error:
            logger.exception("Failed to prepare metrics update for session %s: %s", session_id, error)
            metrics_update = None
            entry_extra = None

        if metrics_update:
            try:
                self.session_store.update_metrics(session_id, metrics_update)
            except Exception as error:
                logger.exception("Failed to update metrics for session %s: %s", session_id, error)

        try:
            self.session_store.append_entry(
                session_id,
                prompt=prompt,
                result=output_text,
                entry_type=entry_type,
                extra=entry_extra,
            )
        except Exception as error:
            logger.exception("Failed to persist Codex activity for session %s: %s", session_id, error)

        self._refresh_warning_cache(session_id)
        return self._maybe_emit_usage_warnings(session_id)
