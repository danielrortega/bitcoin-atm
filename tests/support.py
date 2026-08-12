"""Infraestrutura compartilhada pelos testes.

Três responsabilidades:

1. Tornar `src/` importável e substituir por stubs as dependências de hardware
   (PyQt5, pyserial, escpos, telegram-send) que não existem numa máquina de
   desenvolvimento. Só entram stubs para o que NÃO estiver instalado — no
   Raspberry Pi, com tudo presente, os módulos reais são usados.

2. Gerar fixtures (endereços Base58Check e invoices BOLT11) com codificadores
   de REFERÊNCIA escritos a partir das especificações (BIP-173), independentes
   do decodificador sob teste. Vetores de endereço digitados de memória são uma
   fonte clássica de teste errado: um checksum inválido faz o teste "provar"
   uma rejeição que o código não deveria fazer.

3. Montar uma BTMWindow sem Qt, para exercitar a lógica pura de cédulas
   (contagem, teto, enquadramento) sem abrir janela nem porta serial.
"""

import hashlib
import importlib
import logging
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, 'src'))


# ---------------------------------------------------------------------------
# 1. Stubs das dependências ausentes
# ---------------------------------------------------------------------------
def _stub_if_missing(name, build):
    try:
        importlib.import_module(name)
    except ImportError:
        for mod_name, mod in build().items():
            sys.modules[mod_name] = mod


def _build_telegram():
    m = types.ModuleType('telegram_send')
    m.send = lambda **kwargs: None
    return {'telegram_send': m}


def _build_escpos():
    pkg = types.ModuleType('escpos')
    printer = types.ModuleType('escpos.printer')

    class Usb:
        def __init__(self, *a, **k):
            raise RuntimeError('impressora ausente (stub de teste)')

    printer.Usb = Usb
    pkg.printer = printer
    return {'escpos': pkg, 'escpos.printer': printer}


def _build_serial():
    m = types.ModuleType('serial')

    class Serial:
        def __init__(self, *a, **k):
            raise RuntimeError('porta serial ausente (stub de teste)')

    m.Serial = Serial
    return {'serial': m}


def _build_pyqt5():
    """Stub mínimo do PyQt5: só o suficiente para o import de atm_gui e para
    a criação das classes. Nenhum teste instancia widget — a lógica exercitada
    (_parse_notes, _credit_note, check_note) não toca em Qt."""
    pkg = types.ModuleType('PyQt5')
    widgets = types.ModuleType('PyQt5.QtWidgets')
    core = types.ModuleType('PyQt5.QtCore')
    gui = types.ModuleType('PyQt5.QtGui')

    for name in ('QMainWindow', 'QLabel', 'QPushButton', 'QVBoxLayout',
                 'QHBoxLayout', 'QWidget', 'QMessageBox', 'QLineEdit'):
        setattr(widgets, name, type(name, (), {}))
    for name in ('QTimer', 'Qt', 'QThreadPool', 'QRunnable', 'QObject'):
        setattr(core, name, type(name, (), {}))
    core.pyqtSignal = lambda *a, **k: None
    gui.QFont = type('QFont', (), {'Bold': 75})

    pkg.QtWidgets, pkg.QtCore, pkg.QtGui = widgets, core, gui
    return {'PyQt5': pkg, 'PyQt5.QtWidgets': widgets,
            'PyQt5.QtCore': core, 'PyQt5.QtGui': gui}


_stub_if_missing('telegram_send', _build_telegram)
_stub_if_missing('escpos.printer', _build_escpos)
_stub_if_missing('serial', _build_serial)
_stub_if_missing('PyQt5.QtWidgets', _build_pyqt5)


def silence_logging():
    """Instala um NullHandler ANTES de atm_core ser importado. Com um handler
    já presente, o logging.basicConfig do topo de atm_core vira no-op e não
    tenta abrir /var/log/btc_atm.log — o que na máquina de desenvolvimento
    imprimiria um aviso em toda execução da suíte.

    assertLogs continua funcionando: ele instala o próprio handler e ajusta o
    nível do logger durante o bloco."""
    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)
    root.addHandler(logging.NullHandler())


silence_logging()


# ---------------------------------------------------------------------------
# 2. Codificadores de referência (geradores de fixture)
# ---------------------------------------------------------------------------
_B58_ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


def b58check_encode(version, payload):
    """Codificador Base58Check de referência. `version` é o version byte
    (0x00 P2PKH mainnet, 0x05 P2SH mainnet, 0x6F P2PKH testnet, 0xC4 P2SH
    testnet) e `payload` são os 20 bytes do hash."""
    raw = bytes([version]) + payload
    raw += hashlib.sha256(hashlib.sha256(raw).digest()).digest()[:4]
    num = int.from_bytes(raw, 'big')
    out = ''
    while num:
        num, rem = divmod(num, 58)
        out = _B58_ALPHABET[rem] + out
    n_pad = len(raw) - len(raw.lstrip(b'\x00'))
    return '1' * n_pad + out


_BECH32_CHARSET = 'qpzry9x8gf2tvdw0s3jn54khce6mua7l'


def _bech32_polymod(values):
    gen = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1ffffff) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if ((top >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp):
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def bech32_encode(hrp, data):
    """Codificador bech32 de referência (BIP-173, constante 1)."""
    chk = _bech32_polymod(_bech32_hrp_expand(hrp) + data + [0] * 6) ^ 1
    combined = data + [(chk >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + '1' + ''.join(_BECH32_CHARSET[d] for d in combined)


# Corpo arbitrário; os testes de invoice só verificam HRP e checksum, nunca os
# campos internos do BOLT11 (que o código sob teste também não decodifica).
_INVOICE_BODY = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10] * 6


def make_invoice(hrp='lnbc2500u'):
    """Invoice BOLT11 sintética com checksum bech32 válido.
    'lnbc2500u' = 2500 micro-BTC = 250.000.000 msat = 250.000 sat."""
    return bech32_encode(hrp, _INVOICE_BODY)


def corrupt_checksum(encoded):
    """Troca o último caractere, invalidando o checksum."""
    last = encoded[-1]
    return encoded[:-1] + ('q' if last != 'q' else 'p')


# ---------------------------------------------------------------------------
# 3. BTMWindow sem Qt
# ---------------------------------------------------------------------------
class FakeLabel:
    def __init__(self):
        self.text = ''
        self.style = ''

    def setText(self, t):
        self.text = t

    def setStyleSheet(self, s):
        self.style = s


class FakeButton:
    def __init__(self):
        self.enabled = False

    def setEnabled(self, v):
        self.enabled = v


class FakeInput(FakeButton):
    def __init__(self):
        super().__init__()
        self.text = ''
        self.visible = False

    def clear(self):
        self.text = ''

    def setVisible(self, v):
        self.visible = v


class FakeNoteReader:
    """Noteiro serial falso. `polls` é a lista de buffers devolvidos, um por
    chamada de check_note — modela a janela de leitura de 1 segundo."""

    def __init__(self, polls=()):
        self.polls = list(polls)

    @property
    def in_waiting(self):
        return len(self.polls[0]) if self.polls else 0

    def read(self, n):
        return self.polls.pop(0) if self.polls else b''


def make_window(polls=(), max_transaction_brl=1000, **overrides):
    """BTMWindow sem __init__ (sem QApplication, sem porta serial), com os
    atributos que a lógica de cédulas usa."""
    import atm_gui
    win = atm_gui.BTMWindow.__new__(atm_gui.BTMWindow)
    win.note_reader = FakeNoteReader(polls)
    win.max_transaction_brl = max_transaction_brl
    win.network = 'mainnet'
    win.amount_brl = None
    win.start_time = None
    win.destination = None
    win.payment_type = None
    win._payment_in_flight = False
    win._note_buf = b''
    win.status_label = FakeLabel()
    win.instruction_label = FakeLabel()
    win.onchain_button = FakeButton()
    win.lightning_button = FakeButton()
    win.confirm_button = FakeButton()
    win.address_input = FakeInput()
    win.operated_rate = 500000.0
    win._pending = None
    win.update_rate = lambda: None
    win.check_qr_input = lambda: None
    for k, v in overrides.items():
        setattr(win, k, v)
    return win
