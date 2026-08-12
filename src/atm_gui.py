import logging
import time

from PyQt5.QtWidgets import (QMainWindow, QLabel, QPushButton, QVBoxLayout,
                              QHBoxLayout, QWidget, QMessageBox, QLineEdit)
from PyQt5.QtCore import QTimer, Qt, QThreadPool, QRunnable, QObject, pyqtSignal
from PyQt5.QtGui import QFont
from atm_core import (init_note_reader, get_btc_rate, get_max_transaction_brl,
                      send_onchain_payment, send_lightning_payment,
                      print_receipt, enqueue_transaction, brl_to_btc,
                      PaymentNotBroadcast)
from utils import is_valid_bitcoin_address, is_valid_lightning_destination


# --------------------------------------------------------------------------
# Infra de threading: roda funções bloqueantes (rede/USB) fora da thread de
# eventos da GUI, evitando que a interface congele durante chamadas ao
# BTCPay Server (cotação ~10s, pagamento ~30s) ou à impressora.
# --------------------------------------------------------------------------
class _WorkerSignals(QObject):
    result = pyqtSignal(object)
    error = pyqtSignal(object)
    finished = pyqtSignal()


class _Worker(QRunnable):
    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = _WorkerSignals()

    def run(self):
        try:
            res = self.fn(*self.args, **self.kwargs)
        except Exception as e:
            # Preserva o erro original; o handler na GUI decide o que fazer.
            self.signals.error.emit(e)
        else:
            self.signals.result.emit(res)
        finally:
            self.signals.finished.emit()


def _execute_payment(amount_brl, destination, payment_type, rate, rate_stale):
    """Executado na thread de trabalho. Faz (se necessário) a cotação fresca,
    o envio e a impressão do recibo — tudo o que bloqueia. Levanta
    PaymentNotBroadcast/PaymentUncertain conforme atm_core para o handler
    classificar com segurança."""
    if rate_stale:
        # Não liquida com cotação > 30s: busca uma fresca. Se falhar,
        # PaymentNotBroadcast faz a transação ser enfileirada com segurança.
        rate = get_btc_rate()
    if not rate:
        raise PaymentNotBroadcast("sem cotação fresca para liquidar")
    if payment_type == "onchain":
        txid = send_onchain_payment(amount_brl, destination, rate)
    else:
        txid = send_lightning_payment(amount_brl, destination, rate)
    amount_btc = brl_to_btc(amount_brl, rate)
    print_receipt(amount_brl, amount_btc, destination, txid)
    return txid


class BTMWindow(QMainWindow):
    # Denominações válidas das cédulas de real (BRL). Qualquer leitura fora
    # deste conjunto é tratada como ruído/erro e descartada — nunca creditada.
    VALID_DENOMINATIONS = frozenset({2, 5, 10, 20, 50, 100, 200})
    # Bytes por cédula no protocolo do noteiro (big-endian). É o contrato usado
    # pela POC com socat; um BV20 real usa framing próprio e exigirá ajuste.
    NOTE_FRAME_BYTES = 2

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Bitcoin ATM")
        self.setGeometry(0, 0, 800, 480)
        self.setStyleSheet("background-color: #f0f0f0;")

        # Configuração do layout
        self.central_widget = QWidget(self)
        self.setCentralWidget(self.central_widget)
        self.layout = QVBoxLayout(self.central_widget)
        self.layout.setAlignment(Qt.AlignCenter)

        # Labels
        self.title_label = QLabel("Bitcoin ATM", self)
        self.title_label.setFont(QFont("Arial", 30, QFont.Bold))
        self.title_label.setAlignment(Qt.AlignCenter)
        self.layout.addWidget(self.title_label)

        self.rate_label = QLabel("Cotação BTC/BRL: Carregando...", self)
        self.rate_label.setFont(QFont("Arial", 20))
        self.layout.addWidget(self.rate_label)

        self.timer_label = QLabel("", self)
        self.timer_label.setFont(QFont("Arial", 16))
        self.layout.addWidget(self.timer_label)

        self.instruction_label = QLabel("Insira uma nota no noteiro", self)
        self.instruction_label.setFont(QFont("Arial", 20))
        self.layout.addWidget(self.instruction_label)

        self.status_label = QLabel("Aguardando...", self)
        self.status_label.setFont(QFont("Arial", 18))
        self.layout.addWidget(self.status_label)

        # Botões
        self.button_layout = QHBoxLayout()
        self.onchain_button = QPushButton("Enviar On-Chain", self)
        self.onchain_button.setFont(QFont("Arial", 16))
        self.onchain_button.setStyleSheet("background-color: #4CAF50; color: white; padding: 10px;")
        self.onchain_button.setEnabled(False)
        self.onchain_button.clicked.connect(lambda: self.select_payment("onchain"))
        self.button_layout.addWidget(self.onchain_button)

        self.lightning_button = QPushButton("Enviar via Lightning", self)
        self.lightning_button.setFont(QFont("Arial", 16))
        self.lightning_button.setStyleSheet("background-color: #4CAF50; color: white; padding: 10px;")
        self.lightning_button.setEnabled(False)
        self.lightning_button.clicked.connect(lambda: self.select_payment("lightning"))
        self.button_layout.addWidget(self.lightning_button)
        self.layout.addLayout(self.button_layout)

        self.address_input = QLineEdit(self)
        self.address_input.setFont(QFont("Arial", 14))
        self.address_input.setPlaceholderText("Escaneie o QR code ou digite o endereço...")
        self.address_input.setVisible(False)
        self.address_input.textChanged.connect(self._on_address_changed)
        self.layout.addWidget(self.address_input)

        self.confirm_button = QPushButton("Confirmar", self)
        self.confirm_button.setFont(QFont("Arial", 16))
        self.confirm_button.setStyleSheet("background-color: #2196F3; color: white; padding: 15px;")
        self.confirm_button.setEnabled(False)
        self.confirm_button.clicked.connect(self.confirm_payment)
        self.layout.addWidget(self.confirm_button)

        # Inicializar variáveis
        self.note_reader = init_note_reader()
        self.max_transaction_brl = get_max_transaction_brl()
        self.amount_brl = None
        self.start_time = None
        self.rate_start_time = 0
        self.operated_rate = None
        self.destination = None
        self.payment_type = None
        self._note_buf = b''       # quadro incompleto aguardando o resto

        # Threading
        self.threadpool = QThreadPool()
        self._workers = set()          # mantém referência até finished
        self._rate_in_flight = False
        self._payment_in_flight = False
        self._pending = None           # contexto da transação em voo

        # Timers
        self.rate_timer = QTimer(self)
        self.rate_timer.timeout.connect(self.update_rate_timer)
        self.rate_timer.start(100)

        self.note_timer = QTimer(self)
        self.note_timer.timeout.connect(self.check_note)
        self.note_timer.start(1000)

        self.update_rate()

    # ----------------------------------------------------------------------
    # Helper de execução assíncrona
    # ----------------------------------------------------------------------
    def _run_async(self, fn, on_result, on_error, *args):
        worker = _Worker(fn, *args)
        worker.setAutoDelete(False)
        self._workers.add(worker)
        worker.signals.result.connect(on_result)
        worker.signals.error.connect(on_error)
        # Descarta a referência só depois que result/error já foram tratados
        # (finished é emitido por último), evitando uso após coleta de lixo.
        worker.signals.finished.connect(lambda: self._workers.discard(worker))
        self.threadpool.start(worker)

    # ----------------------------------------------------------------------
    # Cotação (assíncrona)
    # ----------------------------------------------------------------------
    def update_rate(self):
        if self._rate_in_flight:
            return
        self._rate_in_flight = True
        # Reinicia a janela de 30s já no disparo. Junto com o guard acima,
        # evita o loop que, offline, chamava get_btc_rate() ~10x/s.
        self.rate_start_time = time.time()
        self._run_async(get_btc_rate, self._on_rate_result, self._on_rate_error)

    def _on_rate_result(self, rate):
        self._rate_in_flight = False
        if rate:
            self.operated_rate = rate
            self.rate_label.setText(f"Cotação BTC/BRL: R$ {rate:,.2f}")
            if self.amount_brl and not self.destination and not self._payment_in_flight:
                self.status_label.setText(f"Nota detectada: R${self.amount_brl} - Cotação atualizada")
        else:
            # Cotação obsoleta não pode ser usada para liquidar um pagamento.
            self.operated_rate = None
            self.rate_label.setText("Cotação indisponível (offline)")

    def _on_rate_error(self, exc):
        self._rate_in_flight = False
        self.operated_rate = None
        self.rate_label.setText("Cotação indisponível (offline)")

    def update_rate_timer(self):
        if not self.rate_start_time:
            return
        elapsed = time.time() - self.rate_start_time
        remaining = 30 - elapsed
        if remaining <= 0:
            self.update_rate()
            remaining = 30
        self.timer_label.setText(f"Cotação atualiza em: {remaining:.1f}s")

    # ----------------------------------------------------------------------
    # Leitura de notas
    # ----------------------------------------------------------------------
    def check_note(self):
        # SEMPRE drena o buffer serial primeiro — inclusive durante um pagamento.
        # Se só drenássemos quando ocioso, os bytes que chegam enquanto o
        # pagamento roda ficariam no buffer e seriam creditados como uma "nota
        # fantasma" logo após o reset. A leitura roda dentro de um slot de
        # QTimer: uma exceção aqui (noteiro desconectado) abortaria o processo
        # (qFatal do PyQt5) e, com Restart=always, entraria em crash-loop —
        # por isso é protegida.
        try:
            pending = self.note_reader.in_waiting
            data = self.note_reader.read(pending) if pending > 0 else b''
        except Exception as e:
            logging.error("Falha ao ler o noteiro serial: %s", e)
            return

        if self._payment_in_flight:
            return  # buffer já drenado; ignora qualquer entrada durante o envio

        if data:
            for note_value in self._parse_notes(data):
                self._credit_note(note_value)
            return

        if self.start_time and (time.time() - self.start_time > 30) and not self.destination:
            self.status_label.setText(f"Total inserido: R${self.amount_brl} - Cotação atualizada")
            self.update_rate()
            self.start_time = time.time()
        elif self.destination:
            self.check_qr_input()

    def _credit_note(self, note_value):
        """Credita uma cédula válida, ACUMULANDO com as anteriores. Cédulas
        podem ser inseridas a qualquer momento antes de confirmar o pagamento
        (inclusive depois de escolher o método) — antes, uma segunda cédula
        sobrescrevia o total ou era silenciosamente descartada.

        Aplica o teto por transação (max_transaction_brl): atingido o teto, a
        aceitação para e novas cédulas NÃO são creditadas — apenas logadas em
        nível crítico para reembolso manual pelo operador. O teto pode ser
        excedido por no máximo uma cédula (a que o cruza), pois a cédula já
        está fisicamente dentro da máquina quando o valor é lido; o bloqueio
        por hardware (inibir o noteiro) é o complemento recomendado."""
        if self.amount_brl and self.amount_brl >= self.max_transaction_brl:
            logging.critical(
                "Nota de R$%s recusada: teto de R$%s por transação já atingido "
                "(total R$%s). REEMBOLSO MANUAL NECESSÁRIO.",
                note_value, self.max_transaction_brl, self.amount_brl)
            self.status_label.setText(
                f"Limite de R${self.max_transaction_brl} atingido — "
                "não insira mais notas. Procure o operador.")
            self.status_label.setStyleSheet("color: red;")
            return
        self.amount_brl = (self.amount_brl or 0) + note_value
        self.start_time = time.time()
        self.status_label.setText(f"Total inserido: R${self.amount_brl}")
        self.status_label.setStyleSheet("color: black;")
        if self.payment_type is None:
            self.instruction_label.setText(
                "Insira mais notas ou escolha o método de envio")
            self.onchain_button.setEnabled(True)
            self.lightning_button.setEnabled(True)
        if self.amount_brl >= self.max_transaction_brl:
            self.instruction_label.setText(
                f"Limite de R${self.max_transaction_brl} atingido — "
                "escolha o método de envio")

    def _parse_notes(self, data):
        """Interpreta o fluxo do noteiro como uma sequência de quadros de
        NOTE_FRAME_BYTES bytes, um por cédula, e valida cada valor contra as
        denominações reais de cédulas BRL.

        O enquadramento importa nas duas pontas:

        - Duas notas na mesma janela de leitura (1s) chegam como dois quadros
          no mesmo buffer. Interpretar o buffer inteiro com um único
          int.from_bytes daria um valor absurdo — que, sem whitelist, seria
          creditado como R$ milhões e convertido em Bitcoin real; e, com
          whitelist, faria as DUAS notas serem engolidas sem crédito.

        - Uma nota só pode ter o quadro PARTIDO entre duas leituras: nada
          garante que os 2 bytes cheguem no mesmo poll. Descartar cada metade
          por estar desalinhada engolia a cédula inteira, que já estava dentro
          da máquina. Por isso o que sobra fica guardado para a leitura
          seguinte, e não é descartado de imediato.

        Um par de bytes que não corresponde a nenhuma denominação faz o fluxo
        avançar UM byte e tentar de novo, em vez de descartar o par inteiro.
        Essa ressincronização é o que impede um único byte de ruído de deslocar
        todos os quadros seguintes — sem ela, cada cédula posterior seria lida
        metade com metade, rejeitada pela whitelist e engolida em silêncio.

        Ressincronizar não pode creditar uma nota que não existe: toda
        denominação válida tem o byte alto 0x00, e uma leitura deslocada sempre
        põe o byte baixo do quadro anterior nessa posição."""
        buf = self._note_buf + data
        notes = []
        while len(buf) >= self.NOTE_FRAME_BYTES:
            quadro = buf[:self.NOTE_FRAME_BYTES]
            value = int.from_bytes(quadro, "big")
            if value in self.VALID_DENOMINATIONS:
                notes.append(value)
                buf = buf[self.NOTE_FRAME_BYTES:]
            else:
                logging.warning("Quadro inválido do noteiro (%s = %s); "
                                "descartando 1 byte e ressincronizando.",
                                quadro.hex(), value)
                buf = buf[1:]
        # Sobra no máximo NOTE_FRAME_BYTES-1 bytes: um quadro pela metade,
        # que a próxima leitura completa.
        self._note_buf = buf
        return notes

    def _on_address_changed(self, text):
        if self._payment_in_flight:
            return
        self.destination = text.strip() if text.strip() else None
        self.check_qr_input()

    def select_payment(self, payment_type):
        self.payment_type = payment_type
        if payment_type == "lightning":
            self.instruction_label.setText(
                "Escaneie a invoice OU informe seu endereço Lightning "
                "(ex.: voce@walletofsatoshi.com)")
        else:
            self.instruction_label.setText("Escaneie o QR code da sua carteira")
        self.status_label.setText("Aguardando QR code...")
        self.onchain_button.setEnabled(False)
        self.lightning_button.setEnabled(False)
        self.address_input.setVisible(True)
        self.address_input.setFocus()
        self.confirm_button.setEnabled(False)

    def check_qr_input(self):
        if self._payment_in_flight:
            return
        if not self.destination:
            self.confirm_button.setEnabled(False)
            return
        valid = (
            (self.payment_type == "onchain" and is_valid_bitcoin_address(self.destination))
            or (self.payment_type == "lightning" and is_valid_lightning_destination(self.destination))
        )
        if valid:
            self.status_label.setText(f"Endereço detectado: {self.destination[:10]}...")
            self.status_label.setStyleSheet("color: green;")
            self.confirm_button.setEnabled(True)
        else:
            self.status_label.setText("Endereço inválido!")
            self.status_label.setStyleSheet("color: red;")
            self.confirm_button.setEnabled(False)

    # ----------------------------------------------------------------------
    # Pagamento (assíncrono)
    # ----------------------------------------------------------------------
    def confirm_payment(self):
        if self._payment_in_flight or not self.destination:
            if not self.destination:
                QMessageBox.warning(self, "Atenção", "Insira o endereço de destino.")
            return
        # Nunca dispara um pagamento sem dinheiro creditado (estado impossível
        # pelo fluxo normal, mas barato de garantir num caixa com dinheiro real).
        if not self.amount_brl or self.amount_brl <= 0:
            QMessageBox.warning(self, "Atenção", "Nenhum valor inserido.")
            self.reset()
            return

        # Valida o destino (checksum) antes de qualquer envio. Rápido, na GUI.
        if self.payment_type == "onchain":
            valid = is_valid_bitcoin_address(self.destination)
        else:
            valid = is_valid_lightning_destination(self.destination)
        if not valid:
            QMessageBox.warning(self, "Endereço inválido",
                "Endereço/invoice inválido para o método escolhido.")
            self.reset()
            return

        # Cotação obsoleta (>30s) ou inexistente força uma busca fresca dentro
        # da thread de trabalho antes de liquidar.
        rate_stale = (not self.rate_start_time) or (time.time() - self.rate_start_time > 30)
        needs_fresh = rate_stale or not self.operated_rate

        # Dispara o envio na thread de trabalho; a GUI continua responsiva.
        # Sem cotação disponível, _execute_payment levanta PaymentNotBroadcast
        # e a transação é enfileirada com segurança pelo handler de erro.
        self._payment_in_flight = True
        self._pending = (self.amount_brl, self.destination, self.payment_type)
        self._set_busy()
        self._run_async(_execute_payment, self._on_payment_result, self._on_payment_error,
                        self.amount_brl, self.destination, self.payment_type,
                        self.operated_rate, needs_fresh)

    def _set_busy(self):
        self.status_label.setText("Processando pagamento...")
        self.status_label.setStyleSheet("color: #2196F3;")
        self.onchain_button.setEnabled(False)
        self.lightning_button.setEnabled(False)
        self.confirm_button.setEnabled(False)
        self.address_input.setEnabled(False)

    def _on_payment_result(self, txid):
        self._payment_in_flight = False
        # reset() primeiro (restaura o estado ocioso) e a mensagem de
        # resultado depois, para que ela persista até a próxima nota.
        self.reset()
        self.status_label.setText(f"Bitcoin enviado! TxID: {str(txid)[:10]}...")
        self.status_label.setStyleSheet("color: green;")
        self.instruction_label.setText("Transação concluída. Insira outra nota.")

    def _on_payment_error(self, exc):
        self._payment_in_flight = False
        amount_brl, destination, payment_type = self._pending or (self.amount_brl, self.destination, self.payment_type)
        self.reset()
        if isinstance(exc, PaymentNotBroadcast):
            # Com certeza NÃO foi enviado → seguro enfileirar.
            try:
                enqueue_transaction(amount_brl, destination, payment_type, self.operated_rate)
                self.status_label.setText("Sem conexão — transação enfileirada")
                self.status_label.setStyleSheet("color: #e67e22;")
                QMessageBox.information(self, "Enfileirada",
                    f"Não foi possível enviar agora ({exc}).\n\n"
                    "A transação foi enfileirada e será processada automaticamente.")
            except Exception as enq:
                self.status_label.setText("Erro ao enfileirar!")
                self.status_label.setStyleSheet("color: red;")
                QMessageBox.critical(self, "Erro",
                    f"Falha ao enviar e ao enfileirar: {exc}\n{enq}")
        else:
            # Resultado AMBÍGUO (timeout/5xx) ou erro inesperado. O Bitcoin
            # pode já ter sido enviado: NÃO reenfileira, para evitar gasto
            # duplo. Operador precisa conferir manualmente.
            self.status_label.setText("Falha incerta — verifique a carteira!")
            self.status_label.setStyleSheet("color: red;")
            QMessageBox.critical(self, "Verificação necessária",
                f"Falha incerta: {exc}\n\n"
                "O Bitcoin PODE já ter sido enviado. Verifique a carteira no "
                "BTCPay Server antes de reenviar. Nada foi enfileirado "
                "automaticamente para evitar gasto duplo.")

    def reset(self):
        # Restaura o estado ocioso. Handlers de resultado podem sobrescrever
        # instruction/status DEPOIS de chamar reset() para exibir o desfecho.
        self.amount_brl = None
        self.start_time = None
        self.destination = None
        self.payment_type = None
        self._pending = None
        # Um quadro pela metade não pode atravessar para o próximo cliente:
        # combinado com os bytes dele, corromperia a leitura da primeira nota.
        self._note_buf = b''
        self.instruction_label.setText("Insira uma nota no noteiro")
        self.status_label.setText("Aguardando...")
        self.status_label.setStyleSheet("color: black;")
        self.onchain_button.setEnabled(False)
        self.lightning_button.setEnabled(False)
        self.address_input.setEnabled(True)
        self.address_input.clear()
        self.address_input.setVisible(False)
        self.confirm_button.setEnabled(False)
