from PyQt5.QtWidgets import QMainWindow, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QWidget, QMessageBox
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtGui import QFont
from atm_core import (init_note_reader, get_btc_rate, send_onchain_payment, 
                      send_lightning_payment, print_receipt, enqueue_transaction)
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
        rate = get_btc_rate()
        if rate:
            self.operated_rate = rate
            self.rate_label.setText(f"Cotação BTC/BRL: R$ {self.operated_rate:,.2f}")
            self.rate_start_time = time.time()
            if self.amount_brl and not self.destination:
                self.status_label.setText(f"Nota detectada: R${self.amount_brl} - Cotação atualizada")
        else:
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
            data = self.note_reader.read(self.note_reader.in_waiting)
            note_value = int.from_bytes(data, "big") if data else None
            if note_value:
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

    def select_payment(self, payment_type):
        self.payment_type = payment_type
        self.instruction_label.setText("Escaneie o QR code da sua carteira")
        self.status_label.setText("Aguardando QR code...")
        self.onchain_button.setEnabled(False)
        self.lightning_button.setEnabled(False)
        self.confirm_button.setEnabled(True)

    def check_qr_input(self):
        if self.destination and ((self.payment_type == "onchain" and is_valid_bitcoin_address(self.destination)) or 
                                 (self.payment_type == "lightning" and is_valid_lightning_invoice(self.destination))):
            self.status_label.setText(f"Endereço detectado: {self.destination[:10]}...")
            self.status_label.setStyleSheet("color: green;")
        elif self.destination:
            self.status_label.setText("Endereço inválido!")
            self.status_label.setStyleSheet("color: red;")
            self.reset()

    def confirm_payment(self):
        try:
            if time.time() - self.start_time > 30:
                self.update_rate()
                self.start_time = time.time()
            if not self.operated_rate:
                enqueue_transaction(self.amount_brl, self.destination, self.payment_type, self.operated_rate or get_btc_rate())
                QMessageBox.information(self, "Modo Offline", "Transação enfileirada para processamento quando online.")
                self.reset()
                return
            if self.payment_type == "onchain":
                if not is_valid_bitcoin_address(self.destination):
                    raise Exception("Endereço on-chain inválido!")
                txid = send_onchain_payment(self.amount_brl, self.destination, self.operated_rate)
            else:
                if not is_valid_lightning_invoice(self.destination):
                    raise Exception("Invoice Lightning inválido!")
                txid = send_lightning_payment(self.amount_brl, self.destination, self.operated_rate)
            amount_btc = self.amount_brl / self.operated_rate
            self.status_label.setText(f"Bitcoin enviado! TxID: {txid[:10]}...")
            self.instruction_label.setText("Transação concluída. Insira outra nota.")
            print_receipt(self.amount_brl, amount_btc, self.destination, txid)
            self.reset()
        except Exception as e:
            self.status_label.setText("Erro na transação!")
            self.status_label.setStyleSheet("color: red;")
            QMessageBox.critical(self, "Erro", f"Falha na transação: {str(e)}")
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
        self.confirm_button.setEnabled(False)
