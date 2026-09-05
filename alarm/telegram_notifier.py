import os
import json
import time
import html
import logging
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger("infrawatch.telegram")

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "telegram_config.json")

# Asynchronous worker pool for non-blocking notifications
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tg-alert")

# Local timezone offset (WIB / UTC+7 default, can be overridden). A bad value
# (e.g. "UTC+7", empty string) must not crash the whole app at import time --
# fall back to the default instead.
try:
    TZ_OFFSET_HOURS = float(os.environ.get("ALERT_TZ_OFFSET_HOURS", "7"))
except (ValueError, TypeError):
    logger.warning("Invalid ALERT_TZ_OFFSET_HOURS=%r, falling back to 7 (WIB)", os.environ.get("ALERT_TZ_OFFSET_HOURS"))
    TZ_OFFSET_HOURS = 7
LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET_HOURS))


def format_duration(seconds: Optional[float]) -> str:
    """Format duration in seconds to human-readable string (e.g., 2m 15s)."""
    if seconds is None or seconds < 0:
        return "-"
    sec = int(round(seconds))
    if sec < 60:
        return f"{sec}s"
    minutes, sec = divmod(sec, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m {sec}s"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h {minutes}m"


def format_timestamp(epoch_time: Optional[float]) -> str:
    """Format unix epoch timestamp into formatted local time string."""
    try:
        ts = epoch_time if epoch_time is not None else time.time()
        dt = datetime.fromtimestamp(ts, tz=LOCAL_TZ)
        tz_name = "WIB" if TZ_OFFSET_HOURS == 7 else f"UTC{'+' if TZ_OFFSET_HOURS >= 0 else ''}{TZ_OFFSET_HOURS}"
        return dt.strftime(f"%Y-%m-%d %H:%M:%S {tz_name}")
    except Exception:
        return str(epoch_time)


def get_telegram_config() -> Dict[str, Any]:
    """Load telegram configuration from environment variables and JSON config."""
    config = {
        "enabled": True,
        "bot_token": "",
        "chat_id": "",
        "send_firing": True,
        "send_resolved": True,
        # "critical" until a human decides otherwise via PUT /api/telegram —
        # SlowResponse (severity="warning") fires far more often than a real
        # outage and the operator hasn't yet decided whether that's
        # actionable enough for a push notification. Dashboard/Incident
        # History still get every warning either way; only this gate holds
        # back the Telegram push specifically.
        "min_severity": "critical"
    }

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                if isinstance(saved, dict):
                    config.update(saved)
        except Exception as e:
            logger.warning(f"Failed to read {CONFIG_FILE}: {e}")

    # Environment variables take precedence if present
    env_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if env_token:
        config["bot_token"] = env_token.strip()

    env_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if env_chat_id:
        config["chat_id"] = env_chat_id.strip()

    env_enabled = os.environ.get("TELEGRAM_ENABLED")
    if env_enabled is not None:
        config["enabled"] = env_enabled.strip().lower() in ("true", "1", "yes")

    return config


def save_telegram_config(new_config: Dict[str, Any]) -> bool:
    """Persist telegram configuration to telegram_config.json."""
    try:
        current = get_telegram_config()
        current.update(new_config)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"Failed to save telegram config: {e}")
        return False


def send_telegram_raw(bot_token: str, chat_id: str, text: str, parse_mode: str = "HTML") -> Tuple[bool, str]:
    """Synchronous HTTP call to Telegram Bot API sendMessage."""
    if not bot_token or not chat_id:
        return False, "Bot token or Chat ID is not configured"

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "InfraWatch-AlertBot/3.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            resp_body = response.read().decode("utf-8")
            resp_json = json.loads(resp_body)
            if resp_json.get("ok"):
                return True, "Message sent successfully"
            else:
                return False, resp_json.get("description", "Unknown Telegram API error")
    except urllib.error.HTTPError as e:
        err_msg = f"HTTP error {e.code}: {e.reason}"
        try:
            err_body = json.loads(e.read().decode("utf-8"))
            err_msg = f"Telegram API error {e.code}: {err_body.get('description', e.reason)}"
        except Exception:
            pass
        logger.error(f"Telegram alert delivery failed: {err_msg}")
        return False, err_msg
    except Exception as e:
        logger.error(f"Telegram alert delivery connection error: {e}")
        return False, str(e)


def build_alert_message(
    name: str,
    severity: str,
    instance: str,
    summary: str,
    job: str,
    event_time: float,
    is_now_firing: bool,
    duration_seconds: Optional[float] = None,
    latency_ms: Optional[float] = None,
) -> str:
    """Build a clean, structured HTML message for Telegram."""
    safe_instance = html.escape(str(instance or "-"))
    safe_job = html.escape(str(job or "blackbox"))
    time_str = format_timestamp(event_time)

    if is_now_firing:
        table_lines = [
            f"{'Target':<11}{safe_instance}",
            f"{'Job':<11}{safe_job}",
            f"{'Status':<11}UNREACHABLE",
        ]
        if latency_ms is not None:
            try:
                table_lines.append(f"{'Latency':<11}{float(latency_ms):.1f} ms")
            except (ValueError, TypeError):
                table_lines.append(f"{'Latency':<11}{latency_ms} ms")
        table_lines.append(f"{'Time':<11}{time_str}")
        table_content = "\n".join(table_lines)

        return (
            "<b>🔴 InfraWatch — Service Down</b>\n\n"
            f"<pre>{table_content}</pre>\n\n"
            "Investigate host availability."
        )
    else:
        duration_str = format_duration(duration_seconds)
        table_lines = [
            f"{'Target':<11}{safe_instance}",
            f"{'Job':<11}{safe_job}",
            f"{'Status':<11}OPERATIONAL",
            f"{'Downtime':<11}{duration_str}",
        ]
        if latency_ms is not None:
            try:
                table_lines.append(f"{'Latency':<11}{float(latency_ms):.1f} ms")
            except (ValueError, TypeError):
                table_lines.append(f"{'Latency':<11}{latency_ms} ms")
        table_lines.append(f"{'Time':<11}{time_str}")
        table_content = "\n".join(table_lines)

        return (
            "<b>🟢 InfraWatch — Service Restored</b>\n\n"
            f"<pre>{table_content}</pre>"
        )


# Ranks used only to compare against the configured min_severity gate below
# — not a general severity taxonomy, just "how loud is this" ordering.
_SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


def _async_send_worker(
    name: str,
    severity: str,
    instance: str,
    summary: str,
    job: str,
    event_time: float,
    is_now_firing: bool,
    duration_seconds: Optional[float] = None,
    latency_ms: Optional[float] = None
):
    """Background worker function executed in the thread pool."""
    config = get_telegram_config()
    if not config.get("enabled", True):
        return

    bot_token = config.get("bot_token", "").strip()
    chat_id = str(config.get("chat_id", "")).strip()
    if not bot_token or not chat_id:
        return

    if is_now_firing and not config.get("send_firing", True):
        return
    if not is_now_firing and not config.get("send_resolved", True):
        return

    # min_severity gate — this existed as a config field long before it was
    # ever enforced (a saved/GET-able setting that silently did nothing).
    # Unknown severities rank as "critical" (2) so a misconfigured/unlabeled
    # alert fails open (gets sent) rather than silently vanishing.
    min_sev = str(config.get("min_severity", "critical")).strip().lower()
    if _SEVERITY_RANK.get(str(severity).strip().lower(), 2) < _SEVERITY_RANK.get(min_sev, 2):
        return

    text = build_alert_message(
        name=name,
        severity=severity,
        instance=instance,
        summary=summary,
        job=job,
        event_time=event_time,
        is_now_firing=is_now_firing,
        duration_seconds=duration_seconds,
        latency_ms=latency_ms
    )

    success, msg = send_telegram_raw(bot_token, chat_id, text, parse_mode="HTML")
    if success:
        logger.info(f"Telegram alert sent for {instance} (firing={is_now_firing})")
    else:
        logger.warning(f"Telegram alert send failed for {instance}: {msg}")


def dispatch_alert_async(
    name: str,
    severity: str,
    instance: str,
    summary: str,
    job: str,
    event_time: float,
    is_now_firing: bool,
    duration_seconds: Optional[float] = None,
    latency_ms: Optional[float] = None
):
    """Non-blocking asynchronous alert dispatcher. Call from record_alert_event."""
    try:
        _EXECUTOR.submit(
            _async_send_worker,
            name, severity, instance, summary, job,
            event_time, is_now_firing, duration_seconds, latency_ms
        )
    except Exception as e:
        logger.error(f"Failed to submit telegram alert task: {e}")


def test_telegram_connection(bot_token: Optional[str] = None, chat_id: Optional[str] = None) -> Tuple[bool, str]:
    """Send a direct test message to verify token and chat ID."""
    config = get_telegram_config()
    token = (bot_token or config.get("bot_token", "")).strip()
    cid = str(chat_id or config.get("chat_id", "")).strip()

    if not token:
        return False, "Bot Token belum diisi"
    if not cid:
        return False, "Chat ID belum diisi"

    now_str = format_timestamp(time.time())
    table_lines = [
        f"{'Status':<11}CONNECTED",
        f"{'Time':<11}{now_str}",
    ]
    table_content = "\n".join(table_lines)
    text = (
        "<b>🤖 InfraWatch — Test Notification</b>\n\n"
        f"<pre>{table_content}</pre>\n\n"
        "Telegram notification test successful."
    )
    return send_telegram_raw(token, cid, text, parse_mode="HTML")
