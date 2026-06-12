import configparser
import json
import logging
import os
import time
from decimal import Decimal, ROUND_DOWN

import requests
import serial
import telegram_send
from cryptography.fernet import Fernet
from escpos.printer import Usb

_LOG_PATH = '/var/log/btc_atm.log'
_LOG_FORMAT = '%(asctime)s %(levelname)s %(message)s'
try:
    logging.basicConfig(filename=_LOG_PATH, level=logging.INFO, format=_LOG_FORMAT)
except (PermissionError, FileNotFoundError, OSError):
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
    logging.warning("Could not write to %s; logging to stderr instead.", _LOG_PATH)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), '..', 'config.ini')
_QUEUE_PATH = '/var/atm/offline_queue.json'
_KEY_PATH = '/etc/atm/key'

_SATOSHI = Decimal('0.00000001')


class PaymentNotBroadcast(Exception):
    """O pagamento com certeza NÃO foi transmitido (sem conexão, ou rejeitado
    pelo servidor com 4xx). É seguro reenfileirar para tentar de novo."""


class PaymentUncertain(Exception):
    """Resultado ambíguo (timeout, 5xx): o Bitcoin PODE já ter sido enviado.
    NÃO reenfileirar automaticamente — o operador deve conferir a carteira
    antes de qualquer reenvio, para evitar gasto duplo."""


def brl_to_btc(amount_brl, rate):
    """Converte BRL->BTC com precisão decimal, arredondando para baixo
    (ROUND_DOWN) para nunca enviar mais do que o cliente pagou."""
    return (Decimal(str(amount_brl)) / Decimal(str(rate))).quantize(
        _SATOSHI, rounding=ROUND_DOWN)


def _post_payment(url, payload):
    """POST de pagamento com classificação do resultado para segurança
    financeira. Levanta PaymentNotBroadcast (seguro reenfileirar) ou
    PaymentUncertain (não reenfileirar) conforme o tipo de falha."""
    try:
        resp = requests.post(url, headers=_btcpay_headers(), json=payload, timeout=30)
    except requests.ConnectionError as e:
        raise PaymentNotBroadcast(f"sem conexão: {e}") from e
    except requests.Timeout as e:
        raise PaymentUncertain(f"timeout — pode ter sido enviado: {e}") from e
    except requests.RequestException as e:
        raise PaymentUncertain(f"erro de rede: {e}") from e
    if 200 <= resp.status_code < 300:
        return resp
    if 400 <= resp.status_code < 500:
        # Erro de validação/rejeição → não foi transmitido.
        raise PaymentNotBroadcast(f"rejeitado {resp.status_code}: {resp.text[:200]}")
    # 5xx → ambíguo, pode ter transmitido antes de falhar.
    raise PaymentUncertain(f"erro do servidor {resp.status_code}: {resp.text[:200]}")


def _safe_json_field(resp, field, default='desconhecido'):
    """Lê um campo do corpo SEM nunca levantar. Após um 2xx o dinheiro pode
    já ter saído; um erro de parsing não pode propagar (senão a GUI
    reenfileiraria e haveria gasto duplo)."""
    try:
        return resp.json().get(field) or default
    except ValueError:
        return default


def _load_config():
    cfg = configparser.ConfigParser()
    cfg.read(_CONFIG_PATH)
    return cfg


def _get_api_token():
    cfg = _load_config()
    encrypted = cfg['btcpay']['api_token']
    with open(_KEY_PATH, 'rb') as f:
        key = f.read()
    return Fernet(key).decrypt(encrypted.encode()).decode()


def _btcpay_headers():
    return {
        'Authorization': f'token {_get_api_token()}',
        'Content-Type': 'application/json',
    }


def init_note_reader():
    cfg = _load_config()
    port = cfg['hardware']['serial_port']
    baud = int(cfg['hardware']['baud_rate'])
    return serial.Serial(port, baud, timeout=0)


def get_btc_rate():
    cfg = _load_config()
    host = cfg['btcpay']['host'].rstrip('/')
    store_id = cfg['btcpay']['store_id']
    currency = cfg['btcpay'].get('currency', 'BRL')
    pair = f'BTC_{currency}'
    try:
        url = f"{host}/api/v1/stores/{store_id}/rates?currencyPair={pair}"
        resp = requests.get(url, headers=_btcpay_headers(), timeout=10)
        resp.raise_for_status()
        for entry in resp.json():
            if entry.get('currencyPair') == pair:
                # Cada elemento pode trazer 'errors' quando a cotação falhou.
                if entry.get('errors'):
                    logging.error("Rate errors for %s: %s", pair, entry['errors'])
                    return None
                return float(entry['rate'])
    except Exception as e:
        logging.error("Failed to get BTC rate: %s", e)
    return None


def send_onchain_payment(amount_brl, address, rate):
    cfg = _load_config()
    host = cfg['btcpay']['host'].rstrip('/')
    store_id = cfg['btcpay']['store_id']
    crypto = cfg['btcpay'].get('crypto_code', 'BTC')
    # Path correto do Greenfield atual: payment-methods/{BTC-CHAIN}/wallet/...
    # (o antigo /on-chain/{cryptoCode}/transactions retorna 404). Permite
    # sobrescrever via config para instâncias mais antigas.
    pm = cfg['btcpay'].get('onchain_payment_method', f'{crypto}-CHAIN')
    amount_btc = brl_to_btc(amount_brl, rate)
    url = f"{host}/api/v1/stores/{store_id}/payment-methods/{pm}/wallet/transactions"
    payload = {
        'destinations': [
            {'destination': address, 'amount': str(amount_btc), 'subtractFromAmount': False}
        ],
        'noChange': False,
        # 'feerate' é omitido de propósito: enviar null pode causar 422.
        # Ausência → estimativa automática de taxa pelo BTCPay.
    }
    resp = _post_payment(url, payload)
    # 2xx = transmitido. Daqui em diante o parsing nunca pode levantar.
    txid = _safe_json_field(resp, 'transactionHash')
    logging.info("On-chain payment: %s BTC to %s txid=%s", amount_btc, address, txid)
    _send_telegram(amount_brl, amount_btc, address, txid, 'onchain')
    return txid


def send_lightning_payment(amount_brl, invoice, rate):
    cfg = _load_config()
    host = cfg['btcpay']['host'].rstrip('/')
    store_id = cfg['btcpay']['store_id']
    crypto = cfg['btcpay'].get('crypto_code', 'BTC')
    amount_btc = brl_to_btc(amount_brl, rate)
    url = f"{host}/api/v1/stores/{store_id}/lightning/{crypto}/invoices/pay"
    payload = {'BOLT11': invoice}
    resp = _post_payment(url, payload)
    # 200 = Complete, 202 = Pending. Lê o status sem deixar o parsing levantar.
    data = {}
    try:
        data = resp.json()
    except ValueError:
        pass
    status = data.get('status', 'Unknown')
    payment_hash = data.get('paymentHash') or invoice[:20]
    if status == 'Failed':
        # Pagamento Lightning é atômico: 'Failed' = nenhum fundo saiu →
        # seguro reenfileirar.
        raise PaymentNotBroadcast(f"pagamento Lightning falhou: {data or resp.text[:200]}")
    # 'Pending' é tratado como enviado: a invoice é de uso único, então um
    # reenvio do mesmo BOLT11 não causa pagamento duplicado.
    logging.info("Lightning payment: %s BTC status=%s hash=%s", amount_btc, status, payment_hash)
    _send_telegram(amount_brl, amount_btc, invoice, payment_hash, 'lightning')
    return payment_hash


def print_receipt(amount_brl, amount_btc, destination, txid):
    cfg = _load_config()
    usb_id = cfg['hardware']['printer_usb']
    vendor_id, product_id = (int(x, 16) for x in usb_id.split(':'))
    try:
        printer = Usb(vendor_id, product_id)
        printer.text("=== Bitcoin ATM ===\n")
        printer.text(f"Valor: R$ {amount_brl:.2f}\n")
        printer.text(f"BTC: {amount_btc:.8f}\n")
        printer.text(f"Destino: {destination[:30]}...\n")
        printer.text(f"TxID: {txid[:30]}...\n")
        printer.text(f"Data: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        printer.cut()
    except Exception as e:
        logging.error("Failed to print receipt: %s", e)


def enqueue_transaction(amount_brl, destination, payment_type, rate):
    queue = _load_queue()
    queue.append({
        'amount_brl': amount_brl,
        'destination': destination,
        'payment_type': payment_type,
        'rate': rate,
        'timestamp': time.time(),
    })
    _save_queue(queue)
    logging.info("Transaction enqueued: %s BRL to %s (%s)", amount_brl, destination, payment_type)


def process_offline_queue():
    queue = _load_queue()
    if not queue:
        return
    remaining = []
    for tx in queue:
        try:
            rate = tx.get('rate') or get_btc_rate()
            if not rate:
                remaining.append(tx)
                continue
            if tx['payment_type'] == 'onchain':
                txid = send_onchain_payment(tx['amount_brl'], tx['destination'], rate)
            else:
                txid = send_lightning_payment(tx['amount_brl'], tx['destination'], rate)
            amount_btc = brl_to_btc(tx['amount_brl'], rate)
            print_receipt(tx['amount_brl'], amount_btc, tx['destination'], txid)
            logging.info("Offline queue tx processed: %s", txid)
        except PaymentUncertain as e:
            # Resultado ambíguo: não recoloca na fila para não arriscar
            # gasto duplo num reprocessamento futuro. Operador deve conferir.
            logging.error("Queued tx UNCERTAIN, dropped to avoid double-spend: %s", e)
        except Exception as e:
            logging.error("Failed to process queued tx: %s", e)
            remaining.append(tx)
    _save_queue(remaining)


def _load_queue():
    if not os.path.exists(_QUEUE_PATH):
        return []
    try:
        with open(_QUEUE_PATH, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def _save_queue(queue):
    os.makedirs(os.path.dirname(_QUEUE_PATH), exist_ok=True)
    with open(_QUEUE_PATH, 'w') as f:
        json.dump(queue, f)


def _send_telegram(amount_brl, amount_btc, destination, txid, payment_type):
    try:
        msg = (
            f"[Bitcoin ATM] Nova transação\n"
            f"Tipo: {payment_type}\n"
            f"Valor: R$ {amount_brl:.2f}\n"
            f"BTC: {amount_btc:.8f}\n"
            f"Destino: {destination[:30]}...\n"
            f"TxID: {txid}"
        )
        telegram_send.send(messages=[msg])
    except Exception as e:
        logging.warning("Telegram notification failed: %s", e)
