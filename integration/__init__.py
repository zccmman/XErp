"""跨产品集成层。

方向铁律：本层只允许 ``import kernel``（integration → kernel），绝不反向；
``kernel/**`` 不得 import 本层（由 test_integration_acl 钉死 kernel 内部 ACL，
本层由 tests/test_workbuddy_project.py 钉死「零宿主依赖」）。
"""
