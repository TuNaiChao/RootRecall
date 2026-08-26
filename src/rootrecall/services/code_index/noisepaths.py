"""噪声路径判定:测试/仿真基建 + autotools 生成文件。

共享给两个消费方(2026-08-26 实测教训,同一病灶两处症状):
- retrieval._testinfra_prior:检索重排的软降先验(bluez 问「连接流程」top-6 全是
  emulator/android 外围,核心入口 device_connect_le 挤不进);
- code_graph 的 exclude_tests:repo_overview/repo_map 的 hub 排行被测试文件
  (mgmt-tester 474 入边)和生成文件(ltmain.sh 度 1475)霸榜。

两类清单分开暴露:检索只软降**测试基建**(is_testinfra_path);图侧排除**测试基建+
生成文件**(is_noise_path)—— 生成文件(ltmain.sh/configure)在图里的度数污染最重,
但检索侧暂不动(没有实测检索失败来自它们,不瞎调)。
"""

from __future__ import annotations

# 目录段:路径按 / 切段后,任一目录段**精确**命中即测试/仿真/示例基建。
# 不用 startswith("test"):pytest 的 tmp 目录就叫 test_<用例名>0 —— 前缀判定会把整个
# 夹具仓(乃至任何 testmode/testbench 类产品目录)全判成测试基建(2026-08-26 自家夹具抓的)。
TEST_SEGMENTS = frozenset({
    "test", "tests", "testing", "testdata", "testutil", "testutils", "__tests__",
    "emulator", "example", "examples", "demo",
    "mock", "stub", "benchmark", "bench", "unit", "units",
})

# 生成文件(autotools aux / configure 产物):手写的是 configure.ac(不在列),这些是
# autoreconf 生成的巨无霸,图里度数虚高。按 basename 精确匹配,不做子串。
GENERATED_BASENAMES = frozenset({
    "ltmain.sh", "configure", "config.guess", "config.sub",
    "install-sh", "compile", "missing", "depcomp", "ylwrap",
})


def is_testinfra_path(path: str) -> bool:
    """路径含测试/仿真/示例基建 → True。

    判定:任一目录段**精确**命中 TEST_SEGMENTS(不用前缀 —— pytest tmp 目录名就叫
    test_<用例名>,前缀会误杀整个夹具仓);或文件名主干以 -test/_test/-tester/_tester/
    -mock/_mock 结尾(mgmt-tester.c、unit_test.py)。裸 endswith("test") 同理会误伤
    latest/greatest 这类正常单词(2026-08-26 自家反例测试抓的),只认显式分隔符形态。
    """
    if not path:
        return False
    parts = path.replace("\\", "/").lower().split("/")
    for seg in parts[:-1]:
        if seg in TEST_SEGMENTS:
            return True
    stem = parts[-1].rsplit(".", 1)[0]
    # 裸 endswith("test") 会误伤 latest/greatest 这类正常单词(2026-08-26 自家反例测试抓的),
    # 只认两类显式形态:分隔符后缀(-test/_test/-tester/…);以及**文件主干就叫**
    # test/tester 的(远端复测残留:bluez 的 src/shared/tester.c、ell/tester.c 是测试辅助库,
    # 产品代码不会给自己的核心文件起名叫 tester.c)。
    if stem in ("test", "tests", "tester", "mock", "stub"):
        return True
    return any(stem.endswith(s) for s in ("-test", "_test", "-tester", "_tester", "-mock", "_mock"))


def is_noise_path(path: str) -> bool:
    """测试基建**或**生成文件 → True(图侧 hub/repo_map 过滤用)。"""
    if is_testinfra_path(path):
        return True
    if not path:
        return False
    return path.replace("\\", "/").rsplit("/", 1)[-1].lower() in GENERATED_BASENAMES
