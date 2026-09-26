"""An interpreter older than Python 3.10 gets a clear message, not a traceback."""
import subprocess
import sys
import unittest

from tests.fixtures import CHECKOUT

OLD = "import sys; sys.version_info = (3, 8, 10, 'final', 0); sys.version = '3.8.10 (fixture)'; "


class PythonVersionTests(unittest.TestCase):
    def run_code(self, code):
        return subprocess.run([sys.executable, "-c", code], cwd=CHECKOUT, capture_output=True, text=True)

    def test_an_old_python_is_refused_by_name_before_anything_runs(self):
        for code in (OLD + "import linear_runner.cli",
                     OLD + "import runpy; sys.argv = ['runner.py', '--version']; runpy.run_path('runner.py', run_name='__main__')"):
            with self.subTest(code=code):
                result = self.run_code(code)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr.strip(), f"linear-runner needs Python 3.10 or newer, but {sys.executable} "
                                                        "is Python 3.8.10; run it with a newer python3")
                self.assertNotIn("Traceback", result.stderr)

    def test_a_supported_python_imports_normally(self):
        self.assertEqual(self.run_code("import linear_runner.cli").returncode, 0)
