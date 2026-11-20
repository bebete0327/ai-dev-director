import os
import sys
import shlex
import shutil
from pathlib import Path
from typing import Dict, Any, Optional, Union, List
from core.git_manager import GitManager
from core.process_runner import ProcessTreeRunner, ProcessResult
from core.env_utils import get_sanitized_env

class Verifier:
    """Orchestrator-driven verification executing build, test, and git checks."""

    @staticmethod
    def _execute_cmd(cmd_str: Optional[Union[str, List[str]]], cwd: Path, timeout: float = 120.0) -> Dict[str, Any]:
        if not cmd_str:
            return {"command": None, "executed": False, "passed": True, "output": "No command specified"}
        
        try:
            env = get_sanitized_env()
            # Verify under the user's default Python I/O encoding, not whatever the Director process was launched
            # with: an inherited PYTHONIOENCODING/PYTHONUTF8 once hid a CLI that breaks in a normal cp949 terminal.
            for leaked in ("PYTHONIOENCODING", "PYTHONUTF8", "PYTHONLEGACYWINDOWSSTDIO"):
                env.pop(leaked, None)
            cwd_str = str(cwd)
            current_ppath = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = f"{cwd_str}{os.pathsep}{current_ppath}" if current_ppath else cwd_str
            # Prevent stale .pyc hits when Baseline Gate and Builder overwrite a .py file with identical byte length in <1s
            env["PYTHONDONTWRITEBYTECODE"] = "1"

            if isinstance(cmd_str, list):
                argv = list(cmd_str)
            else:
                raw_tokens = shlex.split(cmd_str, posix=False)
                argv = []
                for t in raw_tokens:
                    if len(t) >= 2 and ((t[0] == '"' and t[-1] == '"') or (t[0] == "'" and t[-1] == "'")):
                        argv.append(t[1:-1])
                    else:
                        argv.append(t)

            if not argv:
                return {"command": cmd_str, "executed": False, "passed": True, "output": "Empty command"}

            # Resolve python to current interpreter when running from source; in a frozen .exe, use system Python
            if argv[0] in ("python", "python3"):
                if getattr(sys, "frozen", False):
                    argv[0] = shutil.which(argv[0]) or shutil.which("python") or shutil.which("py") or argv[0]
                else:
                    argv[0] = sys.executable
            else:
                resolved = shutil.which(argv[0])
                if resolved:
                    argv[0] = resolved

            res: ProcessResult = ProcessTreeRunner.run(
                argv=argv,
                cwd=cwd_str,
                env=env,
                timeout=timeout,
                no_output_timeout=None
            )

            if res.timed_out:
                return {
                    "command": cmd_str if isinstance(cmd_str, str) else " ".join(cmd_str),
                    "executed": True,
                    "passed": False,
                    "exit_code": -1,
                    "output": f"Command timed out after {timeout} seconds: {res.timeout_reason}"
                }

            combined_output = (res.stdout or "") + ("\n" + res.stderr if res.stderr else "")
            low_out = combined_output.lower()
            is_pytest_invocation = any("pytest" in str(tok).lower() for tok in argv)
            pytest_missing = "no module named pytest" in low_out
            default_tests_dir_missing = (
                is_pytest_invocation
                and any(str(tok).rstrip("/\\") == "tests" for tok in argv)
                and not (cwd / "tests").is_dir()
                and res.exit_code in (4, 5)
            )
            if res.exit_code != 0 and is_pytest_invocation and (pytest_missing or default_tests_dir_missing):
                base_exe = os.path.basename(str(argv[0])).lower()
                py_exe = (
                    argv[0]
                    if ("python" in base_exe or base_exe in ("py", "py.exe"))
                    else (shutil.which("python") or shutil.which("python3") or sys.executable)
                )
                if not pytest_missing and default_tests_dir_missing:
                    # pytest IS installed, just tests/ folder doesn't exist (tests are at root or subpackages)
                    pt_argv = [py_exe, "-m", "pytest"]
                    pt_res: ProcessResult = ProcessTreeRunner.run(
                        argv=pt_argv,
                        cwd=cwd_str,
                        env=env,
                        timeout=timeout,
                        no_output_timeout=None,
                    )
                    pt_out = ((pt_res.stdout or "") + ("\n" + pt_res.stderr if pt_res.stderr else "")).strip()
                    if not pt_res.timed_out and pt_res.exit_code != 5:
                        return {
                            "command": " ".join(pt_argv),
                            "executed": True,
                            "passed": pt_res.exit_code == 0,
                            "exit_code": pt_res.exit_code,
                            "output": f"[tests/ 폴더 없음 -> 프로젝트 루트 pytest 자동 실행]\n{pt_out}",
                        }
                start_dirs = ["tests", "."] if (cwd / "tests").is_dir() else ["."]
                for sdir in start_dirs:
                    ut_argv = [py_exe, "-m", "unittest", "discover", "-s", sdir, "-p", "test*.py", "-v"]
                    ut_res: ProcessResult = ProcessTreeRunner.run(
                        argv=ut_argv,
                        cwd=cwd_str,
                        env=env,
                        timeout=timeout,
                        no_output_timeout=None,
                    )
                    ut_out = ((ut_res.stdout or "") + ("\n" + ut_res.stderr if ut_res.stderr else "")).strip()
                    if ut_res.timed_out:
                        return {
                            "command": " ".join(ut_argv),
                            "executed": True,
                            "passed": False,
                            "exit_code": -1,
                            "output": f"Command timed out after {timeout} seconds: {ut_res.timeout_reason}",
                        }
                    if "ran 0 tests" in ut_out.lower() and sdir != start_dirs[-1]:
                        continue
                    ut_passed = (ut_res.exit_code == 0) and ("ran 0 tests" not in ut_out.lower())
                    reason_tag = "pytest 미설치 환경" if pytest_missing else "루트 unittest 감지"
                    return {
                        "command": " ".join(ut_argv),
                        "executed": True,
                        "passed": ut_passed,
                        "exit_code": ut_res.exit_code if ut_passed else (ut_res.exit_code or 1),
                        "output": f"[{reason_tag} -> 표준 unittest 자동 실행]\n{ut_out}",
                    }
            passed = (res.exit_code == 0)
            return {
                "command": cmd_str if isinstance(cmd_str, str) else " ".join(cmd_str),
                "executed": True,
                "passed": passed,
                "exit_code": res.exit_code,
                "output": combined_output.strip()
            }
        except Exception as e:
            return {
                "command": cmd_str if isinstance(cmd_str, str) else " ".join(cmd_str),
                "executed": True,
                "passed": False,
                "exit_code": -1,
                "output": f"Execution error: {str(e)}"
            }

    @classmethod
    def verify(
        cls,
        worktree_path: str,
        build_cmd: Optional[str] = None,
        test_cmd: Optional[str] = None,
        lint_cmd: Optional[str] = None,
        timeout: float = 120.0,
        git_checks: bool = True
    ) -> Dict[str, Any]:
        wt = Path(worktree_path).resolve()

        # 1. Git checks
        if git_checks:
            git_status = GitManager.get_status(str(wt))
            git_diff = GitManager.get_diff(str(wt))
        else:                                   # apply-verify on the MAIN tree: never touch its index (no intent-to-add)
            git_status, git_diff = "", ""

        # 2. Run build
        build_res = cls._execute_cmd(build_cmd, wt, timeout=timeout)

        # 3. Run test
        test_res = cls._execute_cmd(test_cmd, wt, timeout=timeout)

        # 4. Run lint
        lint_res = cls._execute_cmd(lint_cmd, wt, timeout=timeout)

        # Overall verification pass condition:
        # Build must pass (if executed)
        # Test must pass (if executed)
        # Lint must pass (if executed)
        overall_passed = build_res["passed"] and test_res["passed"] and lint_res["passed"]

        summary_parts = []
        if build_res["executed"]:
            summary_parts.append(f"Build: {'PASS' if build_res['passed'] else 'FAIL'}")
        if test_res["executed"]:
            summary_parts.append(f"Test: {'PASS' if test_res['passed'] else 'FAIL'}")
        if lint_res["executed"]:
            summary_parts.append(f"Lint: {'PASS' if lint_res['passed'] else 'FAIL'}")
        
        summary = " | ".join(summary_parts) if summary_parts else "No build/test commands configured"

        # Parse test metrics (e.g. 4 passed, Ran 4 tests)
        test_counts = cls._parse_test_counts(test_res.get("output", ""))

        return {
            "passed": overall_passed,
            "summary": summary,
            "build": build_res,
            "test": test_res,
            "lint": lint_res,
            "test_counts": test_counts,
            "git_status": git_status,
            "git_diff": git_diff
        }

    @staticmethod
    def _parse_test_counts(output: str) -> Dict[str, int]:
        import re
        counts = {"passed": 0, "failed": 0, "total": 0}
        if not output:
            return counts

        # Pytest pattern: '12 passed, 1 failed' or '4 passed in 0.02s'
        pass_match = re.search(r"(\d+)\s+passed", output)
        if pass_match:
            counts["passed"] = int(pass_match.group(1))

        fail_match = re.search(r"(\d+)\s+failed", output)
        if fail_match:
            counts["failed"] = int(fail_match.group(1))

        # Unittest pattern: 'Ran 4 tests in 0.001s' or 'Ran 1 test in 0.001s'
        ran_match = re.search(r"Ran\s+(\d+)\s+tests?", output)
        if ran_match:
            counts["total"] = int(ran_match.group(1))
            if re.search(r"^OK\b", output, re.MULTILINE):
                counts["passed"] = counts["total"]
            else:
                f_m = re.search(r"failures=(\d+)", output)
                e_m = re.search(r"errors=(\d+)", output)
                failed = (int(f_m.group(1)) if f_m else 0) + (int(e_m.group(1)) if e_m else 0)
                if failed:
                    counts["failed"] = failed
                    counts["passed"] = max(0, counts["total"] - failed)

        if counts["total"] == 0:
            counts["total"] = counts["passed"] + counts["failed"]

        return counts
