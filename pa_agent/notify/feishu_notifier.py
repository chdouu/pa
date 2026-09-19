"""飞书自定义机器人通知模块.

在下单决策触发时，向飞书群推送交互卡片消息，内容包含：
  - 品种 / 周期 / 方向 / 下单类型 / 入场价 / 止损 / TP1 / TP2
  - 交易置信度 / 预估胜率
  - 决策理由 (decision.reasoning)
  - 下一个市场周期预期及理由 (next_cycle_prediction)

使用方式
--------
1. 在飞书群里添加"自定义机器人"，复制 Webhook URL。
2. （可选）开启签名校验，复制 Secret。
3. 在 config/settings.json 或环境变量中配置 Webhook URL 与可选 Secret。

飞书官方文档
------------
自定义机器人：https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from pa_agent.config.settings import Settings

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_S = 12


# ── 配置加载 ──────────────────────────────────────────────────────────────────
def _feishu_config_dict(settings: "Settings | None" = None) -> dict[str, Any]:
    """Load Feishu settings from settings.json (or an in-memory Settings object)."""
    if settings is not None:
        return settings.feishu.model_dump()
    from pa_agent.config.paths import SETTINGS_JSON_PATH
    from pa_agent.config.settings import load_settings

    return load_settings(SETTINGS_JSON_PATH).feishu.model_dump()


# ── 签名 ──────────────────────────────────────────────────────────────────────
def _gen_sign(secret: str, timestamp: int) -> str:
    """按飞书规范计算 HmacSHA256 + Base64 签名.

    签名字符串：timestamp + "\\n" + secret
    """
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


# ── 辅助格式化 ────────────────────────────────────────────────────────────────
def _fmt(value: Any, default: str = "—") -> str:
    """安全转字符串，空值返回 default."""
    if value is None or value == "":
        return default
    return str(value)


def _truncate(text: str, max_len: int = 500) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "…"


def _direction_style(order_dir: str) -> tuple[str, str]:
    """根据方向返回 (emoji_label, card_color)."""
    d = order_dir.lower()
    if any(k in d for k in ("bull", "多", "long", "buy")):
        return "🟢 做多", "green"
    if any(k in d for k in ("bear", "空", "short", "sell")):
        return "🔴 做空", "red"
    return f"📊 {order_dir}", "blue"


# ── 卡片构建 ──────────────────────────────────────────────────────────────────
def _build_card(
    decision_inner: dict,
    stage2_full: dict,
    symbol: str,
    timeframe: str,
    exchange: str = "",
    bar_time_ms: int | None = None,
    analysis_id: str = "",
    timezone_name: str = "Asia/Taipei",
) -> dict:
    """构建飞书卡片消息体（schema 2.0 interactive 类型）.

    结构：
      header  — 品种/周期 + 方向配色
      section — 下单参数（方向/类型/价格三元组）
      hr + markdown — 决策理由
      hr + markdown — 下一个市场周期预期
      hr + markdown — 关注点（可选）
    """
    dec = decision_inner or {}
    ncp: dict = stage2_full.get("next_cycle_prediction") or {}

    order_type = _fmt(dec.get("order_type"))
    order_dir = _fmt(dec.get("order_direction"))
    entry = _fmt(dec.get("entry_price"))
    stop = _fmt(dec.get("stop_loss_price"))
    tp = _fmt(dec.get("take_profit_price"))
    tp2 = _fmt(dec.get("take_profit_price_2"))
    reasoning = _truncate((dec.get("reasoning") or "").strip(), 600)
    trade_conf = _fmt(dec.get("trade_confidence"))
    win_rate = _fmt(dec.get("estimated_win_rate"))
    watch_points: list = dec.get("watch_points") or []

    dir_label, color = _direction_style(order_dir)

    # 下一个市场周期
    probs: dict = ncp.get("probabilities") or {}
    ncp_reasoning = _truncate((ncp.get("reasoning") or "").strip(), 400)
    if probs:
        best_key = max(probs, key=lambda k: probs[k])
        best_prob = probs[best_key]
        next_cycle_str = f"{best_key}（概率 {best_prob}）"
    elif ncp.get("cycle"):
        next_cycle_str = _fmt(ncp.get("cycle"))
    else:
        next_cycle_str = "—"

    # ── elements ──────────────────────────────────────────────────────────────
    elements: list[dict] = []

    # 摘要行
    elements.append(
        {
            "tag": "markdown",
            "content": (
                f"**品种**：{symbol}　**周期**：{timeframe}\n"
                f"**下单类型**：{order_type}　**方向**：{dir_label}\n"
                f"**入场价**：{entry}　**止损**：{stop}　**TP1**：{tp}　**TP2**：{tp2}\n"
                f"**置信度**：{trade_conf}　**预估胜率**：{win_rate}"
            ),
        }
    )
    audit_parts = []
    if exchange:
        audit_parts.append(f"**交易所**：{exchange}")
    if bar_time_ms is not None:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        audit_parts.append(
            f"**K 棒時間 ({timezone_name})**："
            + datetime.fromtimestamp(bar_time_ms / 1000, tz=ZoneInfo(timezone_name)).isoformat()
        )
    if analysis_id:
        audit_parts.append(f"**分析編號**：{analysis_id}")
    if audit_parts:
        elements.append({"tag": "markdown", "content": "\n".join(audit_parts)})

    # 决策理由
    if reasoning:
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "markdown",
                "content": f"**📝 决策理由**\n{reasoning}",
            }
        )

    # 下一个市场周期预期
    elements.append({"tag": "hr"})
    ncp_block = f"**🔮 下一个市场周期预期**：{next_cycle_str}"
    if ncp_reasoning:
        ncp_block += f"\n{ncp_reasoning}"
    elements.append({"tag": "markdown", "content": ncp_block})

    # 关注点
    if watch_points:
        wp_lines = "\n".join(f"• {_fmt(w)}" for w in watch_points[:5])
        elements.append({"tag": "hr"})
        elements.append(
            {"tag": "markdown", "content": f"**👁 关注点**\n{wp_lines}"}
        )

    card: dict = {
        "schema": "2.0",
        # 飞书卡片 2.0 仅支持 update_multi=true（共享卡片模式）
        "config": {"update_multi": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"🚨 PA Agent 下单信号 — {symbol} {timeframe}",
            },
            "template": color,
            "padding": "12px 12px 12px 12px",
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 12px 12px",
            "elements": elements,
        },
    }
    return {"msg_type": "interactive", "card": card}


# ── 主入口 ────────────────────────────────────────────────────────────────────
def send_order_signal(
    *,
    decision_inner: dict,
    stage2_full: dict,
    symbol: str,
    timeframe: str,
    settings: "Settings | None" = None,
    exchange: str = "",
    bar_time_ms: int | None = None,
    analysis_id: str = "",
    timezone_name: str = "Asia/Taipei",
) -> bool:
    """发送下单信号到飞书群.

    Parameters
    ----------
    decision_inner:
        stage2_decision["decision"] 内层字典。
    stage2_full:
        完整的 stage2_decision 字典（含 next_cycle_prediction）。
    symbol:
        交易品种，如 "XAUUSDm"。
    timeframe:
        K线周期，如 "15m"。
    settings:
        可选的内存 Settings；未传时从 config/settings.json 读取。

    Returns
    -------
    bool
        发送成功返回 True，失败或未启用返回 False。
    """
    cfg = _feishu_config_dict(settings)

    if not cfg.get("enabled", True):
        logger.debug("飞书通知已禁用（settings.json feishu.enabled=false）")
        return False

    webhook_url = (cfg.get("webhook_url") or "").strip()
    if not webhook_url:
        logger.warning(
            "飞书通知：settings.json 未配置 feishu.webhook_url，跳过推送。"
            " 请在菜单「飞书发送通知设置」中完成配置。"
        )
        return False

    try:
        import requests  # type: ignore[import]
    except ImportError:
        logger.warning(
            "飞书通知：requests 库未安装，请运行 pip install requests"
        )
        return False

    # ── 构建消息体 ──────────────────────────────────────────────────────────
    payload = _build_card(
        decision_inner=decision_inner,
        stage2_full=stage2_full,
        symbol=symbol,
        timeframe=timeframe,
        exchange=exchange,
        bar_time_ms=bar_time_ms,
        analysis_id=analysis_id,
        timezone_name=timezone_name,
    )

    # ── 签名（可选）─────────────────────────────────────────────────────────
    secret = (cfg.get("secret") or "").strip()
    if secret:
        ts = int(time.time())
        payload["timestamp"] = str(ts)
        payload["sign"] = _gen_sign(secret, ts)

    # ── 发送 ─────────────────────────────────────────────────────────────────
    try:
        resp = requests.post(
            webhook_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=_REQUEST_TIMEOUT_S,
        )
        result = resp.json()
        # 飞书返回 code=0 或 StatusCode=0 均为成功
        if result.get("code") == 0 or result.get("StatusCode") == 0:
            logger.info(
                "飞书通知发送成功 [%s %s %s]",
                symbol,
                timeframe,
                decision_inner.get("order_type", "?"),
            )
            return True
        else:
            logger.warning(
                "飞书通知返回错误 [%s %s]: %s",
                symbol,
                timeframe,
                result,
            )
            return False
    except Exception as exc:
        logger.warning("飞书通知 HTTP 请求失败: %s", exc)
        return False


def send_trade_event(*, event: str, data: dict[str, Any], settings: "Settings | None" = None) -> bool:
    """Send an OKX lifecycle event without exposing credential fields."""
    cfg = _feishu_config_dict(settings)
    webhook_url = (cfg.get("webhook_url") or "").strip()
    if not cfg.get("enabled", True) or not webhook_url:
        return False
    hidden = {"api_key", "secret", "secret_key", "passphrase"}
    safe = {key: value for key, value in data.items() if key.lower() not in hidden}
    lines = [f"**OKX 交易事件：{event}**"] + [f"**{key}**：{value}" for key, value in safe.items()]
    payload: dict[str, Any] = {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "config": {"update_multi": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"OKX {event}"},
                "template": "blue",
            },
            "body": {"elements": [{"tag": "markdown", "content": "\n".join(lines)}]},
        },
    }
    secret = (cfg.get("secret") or "").strip()
    if secret:
        ts = int(time.time())
        payload["timestamp"] = str(ts)
        payload["sign"] = _gen_sign(secret, ts)
    try:
        import requests  # type: ignore[import]

        result = requests.post(webhook_url, json=payload, timeout=_REQUEST_TIMEOUT_S).json()
        return result.get("code") == 0 or result.get("StatusCode") == 0
    except Exception as exc:
        logger.warning("飞书 OKX 事件通知失败: %s", exc)
        return False
