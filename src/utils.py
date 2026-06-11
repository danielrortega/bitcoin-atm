import qrcode
import requests
from PIL import Image

def generate_qr_code(data):
    qr = qrcode.QRCode(version=1, box_size=6, border=1)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill='black', back_color='white')
    img = img.resize((300, 300), Image.Resampling.LANCZOS)
    img.save("/tmp/qr.png")
    return "/tmp/qr.png"

def is_valid_bitcoin_address(address):
    # Aceita mainnet (1/3/bc1) e também testnet, signet e regtest
    # (m/n/2/tb1/bcrt1), necessários para a POC em testnet.
    # NOTA: validação por prefixo + tamanho apenas. NÃO valida o checksum
    # base58/bech32 — para produção use uma biblioteca como `bech32`/
    # `bitcoinlib` para evitar envio a endereços digitados incorretamente.
    if not isinstance(address, str):
        return False
    addr = address.strip()
    prefixes = ('bc1', 'tb1', 'bcrt1', '1', '3', '2', 'm', 'n')
    return addr.startswith(prefixes) and 14 <= len(addr) <= 100

def is_valid_lightning_invoice(invoice):
    # Aceita mainnet (lnbc), testnet (lntb), signet (lntbs) e regtest (lnbcrt).
    if not isinstance(invoice, str):
        return False
    inv = invoice.strip().lower()
    return inv.startswith(('lnbc', 'lntb', 'lnbcrt')) and len(inv) >= 20

def is_online():
    try:
        requests.get("https://google.com", timeout=5)
        return True
    except requests.RequestException:
        return False
