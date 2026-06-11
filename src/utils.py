import configparser
import os

import qrcode
import requests
from PIL import Image

from btc_address import validate_bitcoin_address, validate_lightning_invoice

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), '..', 'config.ini')


def generate_qr_code(data):
    qr = qrcode.QRCode(version=1, box_size=6, border=1)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill='black', back_color='white')
    img = img.resize((300, 300), Image.Resampling.LANCZOS)
    img.save("/tmp/qr.png")
    return "/tmp/qr.png"


def is_valid_bitcoin_address(address):
    # Validação completa com checksum (Base58Check / Bech32 / Bech32m).
    return validate_bitcoin_address(address)


def is_valid_lightning_invoice(invoice):
    # Validação do checksum bech32 da invoice BOLT11.
    return validate_lightning_invoice(invoice)


def _btcpay_host():
    try:
        cfg = configparser.ConfigParser()
        cfg.read(_CONFIG_PATH)
        return cfg['btcpay']['host'].rstrip('/')
    except Exception:
        return None


def is_online(timeout=5):
    """Verifica a conectividade tentando alcançar o host do BTCPay configurado.
    É mais relevante que um ping genérico (o que importa é se o BTCPay está
    acessível) e evita vazar tráfego a terceiros (ex.: google.com) num
    cenário com Tor. Qualquer resposta HTTP conta como online; só erros de
    conexão/timeout contam como offline. Sem host configurado, retorna False."""
    host = _btcpay_host()
    if not host:
        return False
    try:
        requests.head(host, timeout=timeout, allow_redirects=True)
        return True
    except requests.RequestException:
        return False
