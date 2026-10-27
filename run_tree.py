import os
import subprocess


def run_with_tree_kill(command, *, timeout, input=None, **kwargs):
    """subprocess.run that, on timeout, kills the whole process tree on Windows (taskkill /T /F)."""
    with subprocess.Popen(command, stdin=subprocess.PIPE if input is not None else None,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs) as process:
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True, check=False)
            else:
                process.kill()
            process.communicate()
            raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
