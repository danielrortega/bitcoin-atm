import logging
import sys

from PyQt5.QtCore import QObject, QRunnable, QThreadPool, QTimer, pyqtSignal
from PyQt5.QtWidgets import QApplication

from atm_core import process_offline_queue
from atm_gui import BTMWindow
from utils import is_online


class _WorkerSignals(QObject):
    finished = pyqtSignal()
    error = pyqtSignal(object)


class _Worker(QRunnable):
    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = _WorkerSignals()

    def run(self):
        try:
            self.fn(*self.args, **self.kwargs)
        except Exception as e:
            self.signals.error.emit(e)
        finally:
            self.signals.finished.emit()


def _startup_queue_flush():
    if is_online():
        process_offline_queue()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    threadpool = QThreadPool()

    window = BTMWindow()
    window.showFullScreen()

    def _dispatch_queue_flush():
        worker = _Worker(_startup_queue_flush)
        worker.signals.error.connect(
            lambda e: logging.error("Startup queue flush failed: %s", e)
        )
        threadpool.start(worker)

    # Fire after the event loop starts so the window is visible first.
    QTimer.singleShot(0, _dispatch_queue_flush)

    sys.exit(app.exec_())
