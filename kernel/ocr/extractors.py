"""可插拔发票提取器（P2-03）。

协议极窄：``extract(source) -> InvoiceData``。三个内置实现：

- ``StructuredExtractor``——接收结构化 JSON。上游可以是任何视觉 LLM
  （会话里的多模态 Agent、人工录入、RPA），内核不绑死某家引擎；
- ``VisionLLMExtractor``——openai 兼容视觉接口，按 env 配置启用：
  ``XERP_VISION_BASE_URL`` / ``XERP_VISION_API_KEY`` / ``XERP_VISION_MODEL``；
- ``CompositeExtractor``——先结构化、缺图再走视觉，统一入口。

关键约束：**提取器允许看错，内核不允许收错**——提取结果一律过
``validate_invoice`` 校验 + 发票号查重，不合格的进人工复核队列。
"""

from __future__ import annotations

import base64
import json
import os
import urllib.request
from typing import Any, Protocol

from kernel.ocr.model import InvoiceData

PROMPT_TEMPLATE = """你是发票识别引擎。从图片中提取增值税发票字段，只输出 JSON：
{{"invoice_no": "8位或20位数字", "invoice_date": "YYYY-MM-DD",
  "seller_name": "销方名称", "seller_tax_id": "销方纳税人识别号",
  "total_amount": "价税合计", "net_amount": "不含税金额",
  "tax_amount": "税额", "expense_category": "办公费|差旅费|业务招待费|咨询费"}}
规则：金额保留两位小数的字符串；看不清的字段填空串并降低该字段置信度。"""


class ExtractError(ValueError):
    def __init__(self, code: str, message_zh: str):
        super().__init__(message_zh)
        self.code = code
        self.message_zh = message_zh


class InvoiceExtractor(Protocol):
    def extract(self, source: Any) -> InvoiceData: ...


def _from_dict(data: dict, confidence: dict[str, float] | None = None) -> InvoiceData:
    keys = ("invoice_no", "invoice_date", "seller_name", "seller_tax_id",
            "total_amount", "net_amount", "tax_amount", "expense_category")
    kwargs = {k: str(data.get(k) or "") for k in keys}
    return InvoiceData(**kwargs, confidence=confidence or {})


class StructuredExtractor:
    """结构化通道：上游（视觉 LLM/人工）已完成图片→JSON。"""

    def extract(self, source: dict | str) -> InvoiceData:
        data = json.loads(source) if isinstance(source, str) else source
        if not isinstance(data, dict):
            raise ExtractError("BAD_INVOICE", "发票数据必须是对象")
        if not data.get("invoice_no"):
            raise ExtractError("INVOICE_NO_MISSING", "缺少发票号，无法入账或查重")
        conf = data.get("confidence") if isinstance(data.get("confidence"), dict) else {}
        return _from_dict(data, conf)


class VisionLLMExtractor:
    """视觉 LLM 通道（openai 兼容 chat.completions，base64 图片）。"""

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None):
        self.base_url = (base_url or os.environ.get("XERP_VISION_BASE_URL") or "").rstrip("/")
        self.api_key = api_key or os.environ.get("XERP_VISION_API_KEY") or ""
        self.model = model or os.environ.get("XERP_VISION_MODEL") or ""
        if not (self.base_url and self.api_key and self.model):
            raise ExtractError(
                "VISION_NOT_CONFIGURED",
                "视觉通道未配置：需 XERP_VISION_BASE_URL / XERP_VISION_API_KEY / "
                "XERP_VISION_MODEL 三个环境变量",
            )

    def extract(self, source: bytes | str) -> InvoiceData:
        raw = source if isinstance(source, bytes) else base64.b64decode(source)
        b64 = base64.b64encode(raw).decode()
        body = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT_TEMPLATE},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }],
            "temperature": 0,
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read())
            content = payload["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001 —— 网络/格式错误统一转 ExtractError
            raise ExtractError("VISION_CALL_FAILED", f"视觉提取调用失败：{e}") from e
        # 模型可能带 markdown 代码围栏，剥离后解析
        text = content.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        start, end = text.find("{"), text.rfind("}") + 1
        if start == -1 or end == 0:
            raise ExtractError("VISION_BAD_OUTPUT", f"视觉模型未返回 JSON：{content[:120]}")
        data = json.loads(text[start:end])
        conf = data.pop("confidence", {}) if isinstance(data.get("confidence"), dict) else {}
        return _from_dict(data, conf)


class CompositeExtractor:
    """dict → 结构化通道；bytes/str(非 JSON) → 视觉通道。"""

    def __init__(self, vision: VisionLLMExtractor | None = None):
        self.structured = StructuredExtractor()
        self.vision = vision

    def extract(self, source: Any) -> InvoiceData:
        if isinstance(source, dict):
            return self.structured.extract(source)
        if isinstance(source, str):
            stripped = source.strip()
            if stripped.startswith("{"):
                return self.structured.extract(stripped)
        if self.vision is None:
            try:
                self.vision = VisionLLMExtractor()
            except ExtractError as e:
                raise ExtractError(
                    "NO_EXTRACTOR",
                    f"非结构化输入需要视觉通道：{e.message_zh}",
                ) from e
        return self.vision.extract(source)
