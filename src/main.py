import logging
import sys

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication

from atm_core import process_offline_queue
from atm_gui import BTMWindow, _Worker
from utils import is_online


def _startup_queue_flush():
    """Roda na thread de trabalho: só processa a fila pendente se houver
    conexão. Tanto is_online() quanto process_offline_queue() bloqueiam em
    rede/USB, por isso ficam fora da thread da GUI."""
    if is_online():
        process_offline_queue()


if __name__ == "__main__":
    app = QApplication(sys.argv)

    window = BTMWindow()
    window.showFullScreen()

    # Mantém referência ao worker até terminar, evitando coleta de lixo
    # precoce (mesmo padrão usado em atm_gui._run_async).
    _workers = set()

    def _dispatch_queue_flush():
        worker = _Worker(_startup_queue_flush)
        worker.setAutoDelete(False)
        _workers.add(worker)
        worker.signals.error.connect(
            lambda e: logging.error("Startup queue flush failed: %s", e))
        worker.signals.finished.connect(lambda: _workers.discard(worker))
        # Reaproveita o QThreadPool já criado pela janela.
        window.threadpool.start(worker)

    # Dispara após o loop de eventos iniciar, com a janela já visível, para
    # que a interface apareça imediatamente sem esperar a rede.
    QTimer.singleShot(0, _dispatch_queue_flush)

    sys.exit(app.exec_())
