"""P0-01 冒烟测试：验证包可导入、版本一致。内核规则测试随 P0-05 加入。"""

import kernel


def test_version():
    assert kernel.__version__ == "0.1.0"


def test_package_importable():
    assert callable(getattr(kernel, "__version__", None)) is False  # str, not callable
    assert isinstance(kernel.__version__, str)
