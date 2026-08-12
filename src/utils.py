import configparser
import os

import requests

from btc_address import (DEFAULT_NETWORK, validate_bitcoin_address,
                         validate_lightning_invoice, validate_lightning_address)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), '..', 'config.ini')


def is_valid_bitcoin_address(address, network=DEFAULT_NETWORK):
    # Validação completa com checksum (Base58Check / Bech32 / Bech32m),
    # exigindo a rede configurada.
    return validate_bitcoin_address(address, network)


def is_valid_lightning_destination(destination, network=DEFAULT_NETWORK):
    """Aceita os dois formatos de destino Lightning: uma invoice BOLT11
    (lnbc.../lntb...) ou um Lightning Address (user@domain). O Lightning
    Address é resolvido para uma invoice no momento do pagamento — e a invoice
    resolvida não é checada contra a rede aqui, mas o provedor LNURL só emite
    invoices da rede dele e o BTCPay recusaria uma de outra rede."""
    return (validate_lightning_invoice(destination, network)
            or validate_lightning_address(destination))


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
