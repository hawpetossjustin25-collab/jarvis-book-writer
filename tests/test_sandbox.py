from jarvis_books.llm.sandbox import extract_code, run_tests, unsafe

GOOD = "def add(a, b):\n    return a + b\n"


def test_code_is_taken_from_fenced_blocks_or_the_whole_answer():
    assert extract_code("Here:\n```python\nx = 1\n```\nand\n```\ny = 2\n```") == "x = 1\n\ny = 2"
    assert extract_code("x = 3") == "x = 3"


def test_correct_code_passes_and_wrong_code_is_counted():
    out = run_tests(GOOD, ["assert add(2, 3) == 5", "assert add(-1, 1) == 0"])
    assert out.ok and out.passed == 2
    out = run_tests("def add(a, b):\n    return a - b\n", ["assert add(2, 3) == 5", "assert add(1, 0) == 1"])
    assert out.passed == 1 and not out.ok and "AssertionError" in out.problem


def test_pure_helpers_are_allowed():
    code = "import math\nfrom collections import Counter\nimport csv, io\ndef f(xs):\n    return math.fsum(xs)\n"
    assert unsafe(code) == "" and run_tests(code, ["assert f([1, 2]) == 3"]).ok


def test_code_that_could_touch_the_computer_never_runs(tmp_path):
    marker = tmp_path / "touched"
    for code, why in ((f"import os\nos.mkdir({str(marker)!r})", "imports os"),
                      (f"open({str(marker)!r}, 'w').write('x')", "uses open"),
                      ("import subprocess", "imports subprocess"),
                      ("().__class__.__base__.__subclasses__()", "uses .__subclasses__"),
                      ("x = eval('1')", "uses eval"),
                      ("from . import x", "imports .")):
        out = run_tests(code, ["assert True"])
        assert out.passed == 0 and why in out.problem, (code, out)
    assert not marker.exists()
    assert "uses open" in run_tests(GOOD, ["open('x')"]).problem


def test_broken_and_endless_code_is_reported():
    assert "not valid Python" in run_tests("def (:", ["assert True"]).problem
    out = run_tests("def spin():\n    while True:\n        pass\n", ["spin()"], time_limit_s=2)
    assert out.passed == 0 and "longer than 2 s" in out.problem
