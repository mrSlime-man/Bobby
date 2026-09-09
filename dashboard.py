import atexit
import signal
import time

from src.dashboard.runtime import _signal_process_tree, get_process_info, is_process_running
from src.dashboard.server import app


def _terminate_bot_on_exit() -> None:
    pid = get_process_info().get("pid")
    if not pid or not is_process_running(pid):
        return
    _signal_process_tree(pid, signal.SIGTERM)
    for _ in range(20):
        time.sleep(0.25)
        if not is_process_running(pid):
            return
    _signal_process_tree(pid, signal.SIGKILL)


atexit.register(_terminate_bot_on_exit)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000, timeout_graceful_shutdown=3)
