"""耳朵插件：真听觉 + WebUI 可视化视图（听过什么）。"""

from __future__ import annotations

import asyncio
import base64
import json
import pathlib
import subprocess
import tempfile
import time
import urllib.request

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request

try:
    from astrbot.core.agent.message import TextPart
except Exception:  # 老版本没有
    TextPart = None

DEFAULT_API = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
MARK = "<!--ear-plugin-->"
PLUGIN_NAME = "astrbot_plugin_ear"
HISTORY_LIMIT = 200

PROMPT = (
    "你在替一个听不见声音的人听这条语音。用两三句中文，客观描述：\n"
    "1) 对方说了什么（大意，不必逐字）\n"
    "2) 语气和情绪（平静 / 着急 / 在笑 / 低落 …）\n"
    "3) 语速、音量、有没有停顿\n"
    "4) 有没有背景声（音乐 / 环境噪音 / 风声 …）\n"
    "只说你真的听到的，不要评价，不要编造。"
)


@register(
    PLUGIN_NAME,
    "fy",
    "真听觉：语音先用 Omni 模型听一遍，再把听到的描述交给主模型。",
    "0.2",
    "",
)
class EarPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.cfg = config or {}
        self._data_dir = self._resolve_data_dir()
        self._history_path = self._data_dir / "history.jsonl"
        try:
            context.register_web_api(
                f"/{PLUGIN_NAME}/status", self.api_status, ["GET"], "耳朵状态"
            )
            context.register_web_api(
                f"/{PLUGIN_NAME}/history", self.api_history, ["GET"], "听取记录"
            )
            context.register_web_api(
                f"/{PLUGIN_NAME}/clear", self.api_clear, ["POST"], "清空记录"
            )
            context.register_web_api(
                f"/{PLUGIN_NAME}/settings", self.api_settings, ["GET"], "耳朵配置"
            )
            context.register_web_api(
                f"/{PLUGIN_NAME}/settings/save",
                self.api_settings_save,
                ["POST"],
                "保存耳朵配置",
            )
            logger.info("[ear] 已注册视图接口")
        except Exception as exc:
            logger.warning("[ear] 注册接口失败: %r", exc)
        try:
            from astrbot.core.agent.tool import FunctionTool

            async def _listen_handler(event, url: str = "") -> str:
                import re as _re
                m = _re.search(r"(\d{15,})", url or "")
                if not m:
                    return "这不是一个能识别的抖音作品链接"
                return await self.hear_douyin(m.group(1))

            self._listen_tool = FunctionTool(
                name="ear_listen_douyin",
                description=(
                    "听一条抖音作品的声音：谁在说什么、有没有音乐、音效、情绪。"
                    "用户问「这个视频说了什么」「背景音乐是什么」时用。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "抖音链接或作品 ID"},
                    },
                    "required": ["url"],
                },
                handler=_listen_handler,
                handler_module_path=__name__,
            )
            context.add_llm_tools(self._listen_tool)
            logger.info("[ear] 已注册工具 ear_listen_douyin")
        except Exception as exc:
            logger.warning("[ear] 注册抖音听觉工具失败: %r", exc)

        logger.info("[ear] 耳朵插件已加载")

    # ---------- 数据 ----------
    def _resolve_data_dir(self) -> pathlib.Path:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
            d = pathlib.Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        except Exception:
            d = pathlib.Path(__file__).parent / "data"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _record(self, heard: str, sender: str = "", seconds: float = 0.0) -> None:
        try:
            entry = {
                "ts": int(time.time()),
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "sender": sender,
                "seconds": round(seconds, 1),
                "heard": heard,
            }
            with self._history_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            lines = self._history_path.read_text(encoding="utf-8").splitlines()
            if len(lines) > HISTORY_LIMIT:
                self._history_path.write_text(
                    "\n".join(lines[-HISTORY_LIMIT:]) + "\n", encoding="utf-8"
                )
        except Exception as exc:
            logger.debug("[ear] 记录失败: %r", exc)

    def _read_history(self, limit: int = 50) -> list[dict]:
        try:
            lines = self._history_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return []
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        out.reverse()
        return out

    # ---------- 视图接口 ----------
    async def api_status(self):
        try:
            count = len(self._history_path.read_text(encoding="utf-8").splitlines())
        except Exception:
            count = 0
        return json_response({
            "enabled": bool(self._c("enabled", True)),
            "model": str(self._c("model", "qwen3-omni-flash") or ""),
            "only_owner_private": bool(self._c("only_owner_private", True)),
            "has_key": bool(str(self._c("api_key", "") or "").strip()),
            "count": count,
            "daily_limit": int(self._c("daily_limit", 300) or 0),
            "used_today": self._quota_today()[1],
            "data_dir": str(self._data_dir),
        })

    async def api_history(self):
        return json_response({"items": self._read_history(60)})

    async def api_clear(self):
        try:
            self._history_path.write_text("", encoding="utf-8")
        except Exception as exc:
            return error_response(f"清空失败: {exc}")
        return json_response({"cleared": True})

    async def api_settings(self):
        key = str(self._c("api_key", "") or "")
        return json_response({
            "enabled": bool(self._c("enabled", True)),
            "model": str(self._c("model", "qwen3-omni-flash") or ""),
            "only_owner_private": bool(self._c("only_owner_private", True)),
            "owner_qq": str(self._c("owner_qq", "") or ""),
            "base_url": str(self._c("base_url", "") or ""),
            "douyin_enabled": bool(self._c("douyin_enabled", False)),
            "douyin_cdp_url": str(self._c("douyin_cdp_url", "") or ""),
            "daily_limit": int(self._c("daily_limit", 300) or 0),
            "has_key": bool(key.strip()),
            "key_tail": key[-4:] if key.strip() else "",
        })

    async def api_settings_save(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体格式不对")

        patch: dict = {}
        if isinstance(payload.get("enabled"), bool):
            patch["enabled"] = payload["enabled"]
        if isinstance(payload.get("only_owner_private"), bool):
            patch["only_owner_private"] = payload["only_owner_private"]
        for field in ("model", "owner_qq", "base_url", "douyin_cdp_url"):
            value = payload.get(field)
            if isinstance(value, str):
                patch[field] = value.strip()
        if isinstance(payload.get("douyin_enabled"), bool):
            patch["douyin_enabled"] = payload["douyin_enabled"]
        dl = payload.get("daily_limit")
        if isinstance(dl, (int, float)) and not isinstance(dl, bool):
            patch["daily_limit"] = max(0, int(dl))
        elif isinstance(dl, str) and dl.strip().lstrip("-").isdigit():
            patch["daily_limit"] = max(0, int(dl.strip()))

        if payload.get("clear_key") is True:
            patch["api_key"] = ""
        else:
            new_key = payload.get("api_key")
            if isinstance(new_key, str) and new_key.strip():
                patch["api_key"] = new_key.strip()

        if not patch:
            return error_response("没有要保存的内容")

        try:
            saver = getattr(self.cfg, "save_config_async", None)
            if saver is not None:
                await saver(patch)
            else:
                self.cfg.save_config(patch)
        except Exception as exc:
            logger.warning("[ear] 保存配置失败: %r", exc)
            return error_response(f"保存失败: {exc}")

        for k, v in patch.items():
            try:
                self.cfg[k] = v
            except Exception:
                pass
        logger.info("[ear] 配置已保存: %s", list(patch))
        return json_response({"saved": True, "keys": list(patch)})

    # ---------- 每日额度 ----------
    def _quota_path(self) -> pathlib.Path:
        return self._data_dir / "quota.json"

    def _quota_today(self) -> tuple[str, int]:
        import datetime
        today = datetime.date.today().isoformat()
        try:
            d = json.loads(self._quota_path().read_text(encoding="utf-8"))
        except Exception:
            d = {}
        if d.get("date") != today:
            return today, 0
        return today, int(d.get("count", 0) or 0)

    def _quota_add(self) -> None:
        import datetime
        today = datetime.date.today().isoformat()
        _d, n = self._quota_today()
        try:
            self._quota_path().write_text(
                json.dumps({"date": today, "count": n + 1}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _quota_ok(self) -> bool:
        limit = int(self._c("daily_limit", 300) or 0)
        if limit <= 0:
            return True
        _d, n = self._quota_today()
        return n < limit

    # ---------- 工具 ----------
    def _c(self, key, default=None):
        try:
            return self.cfg.get(key, default)
        except Exception:
            return default

    def _segments(self, event) -> list:
        getters = (
            ("event.message", lambda: event.message),
            ("event.message_obj.message", lambda: event.message_obj.message),
            ("event.get_messages", lambda: event.get_messages()),
        )
        for name, getter in getters:
            try:
                value = getter()
            except Exception:
                continue
            if value:
                return list(value)
        return []

    def _find_record(self, event: AstrMessageEvent):
        for seg in self._segments(event):
            if type(seg).__name__ == "Record":
                return seg
        return None

    def _download(self, url: str) -> pathlib.Path | None:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            tmp = pathlib.Path(tempfile.mkdtemp()) / "voice.bin"
            tmp.write_bytes(data)
            return tmp
        except Exception as exc:
            logger.warning("[ear] 下载语音失败: %r", exc)
            return None

    def _resolve_audio(self, seg) -> pathlib.Path | None:
        for attr in ("path", "file", "url"):
            value = getattr(seg, attr, None)
            if not value:
                continue
            s = str(value).strip()
            if s.startswith("http://") or s.startswith("https://"):
                return self._download(s)
            p = pathlib.Path(s)
            if p.exists():
                return p
        return None

    def _to_wav(self, src: pathlib.Path) -> pathlib.Path | None:
        out = src.with_name("voice_16k.wav")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                 "-t", "60", "-ar", "16000", "-ac", "1", str(out)],
                check=True, timeout=90,
            )
            if out.exists() and out.stat().st_size > 500:
                return out
            return None
        except Exception as exc:
            logger.warning("[ear] 转码失败: %r", exc)
            return None

    def _call_omni(self, wav: pathlib.Path) -> str:
        key = str(self._c("api_key", "") or "").strip()
        model = str(self._c("model", "qwen3-omni-flash") or "").strip()
        if not key:
            logger.warning("[ear] 没配 api_key")
            return ""
        base_url = str(self._c("base_url", "") or "").strip() or DEFAULT_API
        b64 = base64.b64encode(wav.read_bytes()).decode()
        body = {
            "model": model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "input_audio",
                     "input_audio": {"data": "data:;base64," + b64, "format": "wav"}},
                ],
            }],
        }
        req = urllib.request.Request(
            base_url, data=json.dumps(body).encode(),
            headers={"content-type": "application/json", "authorization": "Bearer " + key},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
        return (data["choices"][0]["message"].get("content") or "").strip()

    async def _hear(self, seg) -> tuple[str, float]:
        if not self._quota_ok():
            logger.warning("[ear] 今天的听觉额度用完了，跳过")
            return "", 0.0
        # 下载是阻塞网络 I/O，必须丢到线程里，不能占事件循环
        src = await asyncio.to_thread(self._resolve_audio, seg)
        if src is None:
            logger.warning("[ear] 找不到可用的音频文件")
            return "", 0.0
        wav = await asyncio.to_thread(self._to_wav, src)
        if wav is None:
            return "", 0.0
        try:
            seconds = round(wav.stat().st_size / 32000, 1)
        except Exception:
            seconds = 0.0
        try:
            heard = await asyncio.to_thread(self._call_omni, wav)
        except Exception as exc:
            logger.warning("[ear] Omni 调用失败: %r", exc)
            return "", seconds
        logger.info("[ear] 听到了：%s", heard[:100])
        self._quota_add()
        try:
            wav.unlink(missing_ok=True)
            src.unlink(missing_ok=True)
        except Exception:
            pass
        return heard, seconds


    # ---------- 抖音：听视频的声音 ----------
    async def _grab_douyin_audio_url(self, aweme_id: str) -> tuple[str, dict]:
        """打开抖音视频页，截出 media-audio 那条真实地址。"""
        from playwright.async_api import async_playwright

        cdp = str(self._c("douyin_cdp_url", "") or "").strip()
        if not cdp:
            return "", {}

        hits: list[tuple[str, dict]] = []

        def on_request(req):
            u = req.url
            if ("douyinvod" in u or "/video/tos/" in u) and "audio" in u:
                hits.append((u, dict(req.headers)))

        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(cdp)
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await ctx.new_page()
            page.on("request", on_request)
            try:
                await page.goto(f"https://www.douyin.com/video/{aweme_id}",
                                wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(2500)
                try:
                    await page.evaluate(
                        "() => { const v=document.querySelector('video'); if(v){v.muted=true; v.play().catch(()=>{});} }"
                    )
                except Exception:
                    pass
                for _ in range(15):
                    await page.wait_for_timeout(1000)
                    if hits:
                        break
            finally:
                try:
                    await page.close()
                except Exception:
                    pass
        return hits[0] if hits else ("", {})

    def _fetch_url(self, url: str, headers: dict) -> pathlib.Path | None:
        keep = {k: v for k, v in headers.items()
                if k.lower() in ("referer", "user-agent", "cookie")}
        keep.setdefault("Referer", "https://www.douyin.com/")
        try:
            req = urllib.request.Request(url, headers=keep)
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            p = pathlib.Path(tempfile.mkdtemp()) / "dy_audio.mp4"
            p.write_bytes(data)
            return p
        except Exception as exc:
            logger.warning("[ear] 抖音音频下载失败: %r", exc)
            return None

    async def hear_douyin(self, aweme_id: str) -> str:
        """听一条抖音作品的声音，返回描述。"""
        if not bool(self._c("douyin_enabled", False)):
            return "抖音听觉没开（在配置里打开 douyin_enabled，并填 douyin_cdp_url）"
        url, headers = await self._grab_douyin_audio_url(aweme_id)
        if not url:
            return "没截到音频流：检查 douyin_cdp_url 是不是一个已登录抖音的浏览器调试端口"
        raw = await asyncio.to_thread(self._fetch_url, url, headers)
        if raw is None:
            return "音频下载失败"
        wav = await asyncio.to_thread(self._to_wav, raw)
        if wav is None:
            return "抽音轨失败（可能这条没有声音）"
        try:
            heard = await asyncio.to_thread(self._call_omni, wav)
        except Exception as exc:
            logger.warning("[ear] Omni 调用失败: %r", exc)
            return "听觉模型调用失败"
        logger.info("[ear] 抖音听到了：%s", heard[:100])
        return heard

    # ---------- 注入 ----------
    @filter.on_llm_request(priority=2**31 - 2)
    async def inject_hearing(self, event: AstrMessageEvent, req) -> None:
        if not bool(self._c("enabled", True)):
            return
        seg = self._find_record(event)
        if seg is None:
            return
        if bool(self._c("only_owner_private", True)):
            try:
                if event.get_group_id():
                    return
                owner = str(self._c("owner_qq", "") or "").strip()
                if owner:
                    if str(event.get_sender_id()) != owner:
                        return
                else:
                    # 没填主人号时不能放行所有私聊，退化为仅管理员
                    try:
                        if not event.is_admin():
                            return
                    except Exception:
                        return
            except Exception:
                return
        heard, seconds = await self._hear(seg)
        if not heard:
            return
        try:
            self._record(heard, str(event.get_sender_id()), seconds)
        except Exception:
            pass

        block = f"{MARK}\n[你刚听到了这条语音（这是你亲耳听到的，不是转文字）]\n{heard}"
        try:
            parts = list(getattr(req, "extra_user_content_parts", None) or [])
        except Exception:
            parts = []
        if any(MARK in str(getattr(x, "text", "") or x) for x in parts):
            return

        part = None
        if TextPart is not None:
            try:
                part = TextPart(text=block).mark_as_temp()
            except Exception:
                try:
                    part = TextPart(text=block)
                except Exception:
                    part = None
        if part is None:
            # 拿不到 TextPart 就不注入：往 system_prompt 里追加动态内容会破坏前缀缓存
            logger.warning("[ear] 当前 AstrBot 版本没有 TextPart，跳过本次注入")
            return

        parts.append(part)
        try:
            req.extra_user_content_parts = parts
            logger.info("[ear] 已注入语音描述（%d 段）", len(parts))
        except Exception as exc:
            logger.warning("[ear] 写入失败：%r", exc)
