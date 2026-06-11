import time

from PyQt5.QtWidgets import (QMainWindow, QLabel, QPushButton, QVBoxLayout,
                              QHBoxLayout, QWidget, QMessageBox, QLineEdit)
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtGui import QFont
from atm_core import (init_note_reader, get_btc_rate, send_onchain_payment,
                      send_lightning_payment, print_receipt, enqueue_transaction,
                      brl_to_btc, PaymentNotBroadcast)
from utils import is_valid_bitcoin_address, is_valid_lightning_invoice

class BTMWindow(QMainWindow):
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
        self.amount_brl = None
        self.start_time = None
        self.rate_start_time = 0
        self.operated_rate = None
        self.destination = None
        self.payment_type = None

        # Timers
        self.rate_timer = QTimer(self)
        self.rate_timer.timeout.connect(self.update_rate_timer)
        self.rate_timer.start(100)

        self.note_timer = QTimer(self)
        self.note_timer.timeout.connect(self.check_note)
        self.note_timer.start(1000)

        self.update_rate()

    def update_rate(self):
        # Reinicia a janela de 30s em qualquer caso. Se isto só fosse feito
        # no sucesso, ao perder conexão o update_rate_timer veria
        # remaining<=0 a cada tick (100ms) e chamaria get_btc_rate()
        # (bloqueante, até 10s) ~10x/s, congelando a interface.
        self.rate_start_time = time.time()
        rate = get_btc_rate()
        if rate:
            self.operated_rate = rate
            self.rate_label.setText(f"Cotação BTC/BRL: R$ {self.operated_rate:,.2f}")
            if self.amount_brl and not self.destination:
                self.status_label.setText(f"Nota detectada: R${self.amount_brl} - Cotação atualizada")
        else:
            # Cotação obsoleta não pode ser usada para liquidar um pagamento.
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

    def check_note(self):
        if self.note_reader.in_waiting > 0:
            # Sempre drena o buffer serial para não acumular, mas só aceita
            # uma nova nota enquanto o cliente ainda está escolhendo o método.
            # Pulsos espúrios durante o pagamento em andamento são ignorados.
            data = self.note_reader.read(self.note_reader.in_waiting)
            note_value = int.from_bytes(data, "big") if data else None
            if note_value and self.payment_type is None and self.destination is None:
                self.amount_brl = note_value
                self.start_time = time.time()
                self.status_label.setText(f"Nota detectada: R${note_value}")
                self.instruction_label.setText("Escolha o método de envio")
                self.onchain_button.setEnabled(True)
                self.lightning_button.setEnabled(True)
        elif self.start_time and (time.time() - self.start_time > 30) and not self.destination:
            self.status_label.setText(f"Nota detectada: R${self.amount_brl} - Cotação atualizada")
            self.update_rate()
            self.start_time = time.time()
        elif self.destination:
            self.check_qr_input()

    def _on_address_changed(self, text):
        self.destination = text.strip() if text.strip() else None
        self.check_qr_input()

    def select_payment(self, payment_type):
        self.payment_type = payment_type
        self.instruction_label.setText("Escaneie o QR code da sua carteira")
        self.status_label.setText("Aguardando QR code...")
        self.onchain_button.setEnabled(False)
        self.lightning_button.setEnabled(False)
        self.address_input.setVisible(True)
        self.address_input.setFocus()
        self.confirm_button.setEnabled(False)

    def check_qr_input(self):
        if not self.destination:
            self.confirm_button.setEnabled(False)
            return
        valid = (
            (self.payment_type == "onchain" and is_valid_bitcoin_address(self.destination))
            or (self.payment_type == "lightning" and is_valid_lightning_invoice(self.destination))
        )
        if valid:
            self.status_label.setText(f"Endereço detectado: {self.destination[:10]}...")
            self.status_label.setStyleSheet("color: green;")
            self.confirm_button.setEnabled(True)
        else:
            self.status_label.setText("Endereço inválido!")
            self.status_label.setStyleSheet("color: red;")
            self.confirm_button.setEnabled(False)

    def confirm_payment(self):
        if not self.destination:
            QMessageBox.warning(self, "Atenção", "Insira o endereço de destino.")
            return

        # Revalida a cotação se a janela de 30s expirou.
        if self.start_time and time.time() - self.start_time > 30:
            self.update_rate()
            self.start_time = time.time()

        # Sem cotação (offline) → enfileira para liquidar quando online.
        if not self.operated_rate:
            try:
                enqueue_transaction(self.amount_brl, self.destination, self.payment_type, None)
                QMessageBox.information(self, "Modo Offline",
                    "Sem cotação no momento. Transação enfileirada para "
                    "processamento quando online.")
            except Exception as e:
                QMessageBox.critical(self, "Erro", f"Falha ao enfileirar: {e}")
            self.reset()
            return

        # Valida o destino antes de qualquer envio.
        if self.payment_type == "onchain":
            valid = is_valid_bitcoin_address(self.destination)
        else:
            valid = is_valid_lightning_invoice(self.destination)
        if not valid:
            QMessageBox.warning(self, "Endereço inválido",
                "Endereço/invoice inválido para o método escolhido.")
            self.reset()
            return

        # Tenta enviar.
        try:
            if self.payment_type == "onchain":
                txid = send_onchain_payment(self.amount_brl, self.destination, self.operated_rate)
            else:
                txid = send_lightning_payment(self.amount_brl, self.destination, self.operated_rate)
            amount_btc = brl_to_btc(self.amount_brl, self.operated_rate)
            self.status_label.setText(f"Bitcoin enviado! TxID: {str(txid)[:10]}...")
            self.instruction_label.setText("Transação concluída. Insira outra nota.")
            print_receipt(self.amount_brl, amount_btc, self.destination, txid)
            self.reset()
        except PaymentNotBroadcast as e:
            # Com certeza NÃO foi enviado → seguro enfileirar.
            self.status_label.setText("Sem conexão — transação enfileirada")
            self.status_label.setStyleSheet("color: #e67e22;")
            try:
                enqueue_transaction(self.amount_brl, self.destination, self.payment_type, self.operated_rate)
                QMessageBox.information(self, "Enfileirada",
                    f"Não foi possível enviar agora ({e}).\n\n"
                    "A transação foi enfileirada e será processada automaticamente.")
            except Exception as enq:
                QMessageBox.critical(self, "Erro",
                    f"Falha ao enviar e ao enfileirar: {e}\n{enq}")
            self.reset()
        except Exception as e:
            # Resultado AMBÍGUO (timeout/5xx) ou erro inesperado. O Bitcoin
            # pode já ter sido enviado: NÃO reenfileira, para evitar gasto
            # duplo. Operador precisa conferir manualmente.
            self.status_label.setText("Falha incerta — verifique a carteira!")
            self.status_label.setStyleSheet("color: red;")
            QMessageBox.critical(self, "Verificação necessária",
                f"Falha incerta: {e}\n\n"
                "O Bitcoin PODE já ter sido enviado. Verifique a carteira no "
                "BTCPay Server antes de reenviar. Nada foi enfileirado "
                "automaticamente para evitar gasto duplo.")
            self.reset()

    def reset(self):
        self.amount_brl = None
        self.start_time = None
        self.destination = None
        self.payment_type = None
        self.instruction_label.setText("Insira uma nota no noteiro")
        self.status_label.setText("Aguardando...")
        self.status_label.setStyleSheet("color: black;")
        self.onchain_button.setEnabled(False)
        self.lightning_button.setEnabled(False)
        self.address_input.clear()
        self.address_input.setVisible(False)
        self.confirm_button.setEnabled(False)
