import logging
import sys

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication, QMessageBox

from atm_core import process_offline_queue
from atm_gui import BTMWindow, _Worker
from utils import is_online

# Intervalo de reprocessamento da fila offline enquanto o ATM está rodando.
# Sem isto, a fila só seria drenada no startup — um quiosque 24/7 que raramente
# reinicia deixaria transações pendentes paradas indefinidamente.
_QUEUE_RETRY_MS = 5 * 60 * 1000


def _startup_queue_flush():
    """Roda na thread de trabalho: só processa a fila pendente se houver
    conexão. Tanto is_online() quanto process_offline_queue() bloqueiam em
    rede/USB, por isso ficam fora da thread da GUI."""
    if is_online():
        process_offline_queue()


if __name__ == "__main__":
    app = QApplication(sys.argv)

    try:
        window = BTMWindow()
    except Exception as e:
        # Falha de hardware (noteiro ausente) ou config faltando não pode
        # derrubar o processo com um traceback cru numa tela de quiosque.
        # Registra e mostra uma mensagem clara antes de sair (o systemd, com
        # Restart=always, tentará de novo — agora com o motivo nos logs).
        logging.critical("Falha ao inicializar a interface do ATM: %s", e)
        QMessageBox.critical(None, "Erro ao iniciar o ATM",
            f"Não foi possível inicializar o ATM:\n\n{e}\n\n"
            "Verifique o noteiro, a impressora e o config.ini.")
        sys.exit(1)

    window.showFullScreen()

    # Mantém referência ao worker até terminar, evitando coleta de lixo
    # precoce (mesmo padrão usado em atm_gui._run_async).
    _workers = set()
    _flush_in_flight = {'v': False}

    def _dispatch_queue_flush():
        # Evita flushes concorrentes (o processamento é serializado por um lock
        # em atm_core, mas não faz sentido empilhar workers redundantes).
        if _flush_in_flight['v']:
            return
        _flush_in_flight['v'] = True
        worker = _Worker(_startup_queue_flush)
        worker.setAutoDelete(False)
        _workers.add(worker)
        worker.signals.error.connect(
            lambda e: logging.error("Startup queue flush failed: %s", e))

        def _done():
            _workers.discard(worker)
            _flush_in_flight['v'] = False
        worker.signals.finished.connect(_done)
        # Reaproveita o QThreadPool já criado pela janela.
        window.threadpool.start(worker)

    # Dispara após o loop de eventos iniciar, com a janela já visível, para
    # que a interface apareça imediatamente sem esperar a rede.
    QTimer.singleShot(0, _dispatch_queue_flush)

    # Reprocessa a fila periodicamente enquanto o ATM roda (não só no startup).
    retry_timer = QTimer()
    retry_timer.timeout.connect(_dispatch_queue_flush)
    retry_timer.start(_QUEUE_RETRY_MS)

    sys.exit(app.exec_())
