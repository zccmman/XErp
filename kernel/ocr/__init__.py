"""发票 OCR 入账包（P2-03）：可插拔提取 + 内核校验/查重/管线。"""

from kernel.ocr.extractors import (
    CompositeExtractor,
    ExtractError,
    StructuredExtractor,
    VisionLLMExtractor,
)
from kernel.ocr.model import (
    InvoiceData,
    compare_fields,
    low_confidence_fields,
    validate_invoice,
)
from kernel.ocr.pipeline import PipelineError, accuracy_report, ingest_invoice

__all__ = [
    "CompositeExtractor",
    "ExtractError",
    "InvoiceData",
    "PipelineError",
    "StructuredExtractor",
    "VisionLLMExtractor",
    "accuracy_report",
    "compare_fields",
    "ingest_invoice",
    "low_confidence_fields",
    "validate_invoice",
]
