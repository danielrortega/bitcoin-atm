import configparser
import json
import logging
import os
import time

import requests
import serial
import telegram_send
from cryptography.fernet import Fernet
from escpos.printer import Usb

logging.basicConfig(
    filename='/var/log/btc_atm.log',
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), '..', 'config.ini')
_QUEUE_PATH = '/var/atm/offline_queue.json'
_KEY_PATH = '/etc/atm/key'


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
    host = cfg['btcpay']['host']
    store_id = cfg['btcpay']['store_id']
    currency = cfg['btcpay'].get('currency', 'BRL')
    try:
        url = f"{host}/api/v1/stores/{store_id}/rates?currencyPair=BTC_{currency}"
        resp = requests.get(url, headers=_btcpay_headers(), timeout=10)
        resp.raise_for_status()
        for entry in resp.json():
            if entry.get('currencyPair') == f'BTC_{currency}':
                return float(entry['rate'])
    except Exception as e:
        logging.error("Failed to get BTC rate: %s", e)
    return None


def send_onchain_payment(amount_brl, address, rate):
    cfg = _load_config()
    host = cfg['btcpay']['host']
    store_id = cfg['btcpay']['store_id']
    wallet_id = cfg['btcpay']['wallet_id']
    amount_btc = round(amount_brl / rate, 8)
    url = f"{host}/api/v1/stores/{store_id}/on-chain/{wallet_id}/transactions"
    payload = {
        'destinations': [
            {'destination': address, 'amount': str(amount_btc), 'subtractFromAmount': False}
        ],
        'feerate': None,
        'noChange': False,
    }
    resp = requests.post(url, headers=_btcpay_headers(), json=payload, timeout=30)
    resp.raise_for_status()
    txid = resp.json()['transactionHash']
    logging.info("On-chain payment: %s BTC to %s txid=%s", amount_btc, address, txid)
    _send_telegram(amount_brl, amount_btc, address, txid, 'onchain')
    return txid


def send_lightning_payment(amount_brl, invoice, rate):
    cfg = _load_config()
    host = cfg['btcpay']['host']
    store_id = cfg['btcpay']['store_id']
    lightning_wallet_id = cfg['btcpay']['lightning_wallet_id']
    amount_btc = round(amount_brl / rate, 8)
    url = f"{host}/api/v1/stores/{store_id}/lightning/{lightning_wallet_id}/invoices/pay"
    payload = {'BOLT11': invoice}
    resp = requests.post(url, headers=_btcpay_headers(), json=payload, timeout=30)
    resp.raise_for_status()
    payment_hash = resp.json().get('paymentHash', invoice[:20])
    logging.info("Lightning payment: %s BTC invoice=%s hash=%s", amount_btc, invoice[:20], payment_hash)
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
            amount_btc = tx['amount_brl'] / rate
            print_receipt(tx['amount_brl'], amount_btc, tx['destination'], txid)
            logging.info("Offline queue tx processed: %s", txid)
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
