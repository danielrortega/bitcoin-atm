"""Envio de pagamento: contrato com o BTCPay e classificação de falhas.

A classificação é o coração da segurança financeira do ATM. Cada falha precisa
cair em exatamente um dos dois lados:

  PaymentNotBroadcast -> o Bitcoin com certeza NÃO saiu; reenfileirar é seguro.
  PaymentUncertain    -> pode ter saído; reenfileirar arriscaria gasto duplo.

Errar para o lado errado custa dinheiro nas duas direções: classificar como
incerto o que não foi enviado faz o cliente perder o que pagou; classificar
como não-enviado o que foi enviado paga duas vezes.
"""

import os
import tempfile
import unittest
from decimal import Decimal
from unittest import mock

import requests
import support
from cryptography.fernet import Fernet

import atm_core

ENDERECO = 'bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4'


def resposta(status_code, payload=None, text=''):
    resp = mock.Mock()
    resp.status_code = status_code
    resp.text = text
    if payload is None:
        resp.json.side_effect = ValueError('corpo não é JSON')
    else:
        resp.json.return_value = payload
    return resp


class BaseBTCPay(unittest.TestCase):
    """Config real em diretório temporário, com chave Fernet válida."""

    API_TOKEN = 'token-secreto-do-btcpay'

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.key_path = os.path.join(self.dir, 'key')
        key = Fernet.generate_key()
        with open(self.key_path, 'wb') as f:
            f.write(key)
        self.cifrado = Fernet(key).encrypt(self.API_TOKEN.encode()).decode()
        self.config_path = self.escrever_config(api_token=self.cifrado)

        for alvo, valor in (('_CONFIG_PATH', self.config_path),
                            ('_KEY_PATH', self.key_path)):
            p = mock.patch.object(atm_core, alvo, valor)
            p.start()
            self.addCleanup(p.stop)

        # Nenhum teste pode notificar de verdade. O mock fica na fronteira com
        # a biblioteca (telegram_send.send), não em _send_telegram — assim o
        # tratamento de erro do próprio _send_telegram continua sob teste.
        p = mock.patch.object(atm_core.telegram_send, 'send')
        self.telegram = p.start()
        self.addCleanup(p.stop)

    def escrever_config(self, api_token, extra=''):
        path = os.path.join(self.dir, 'config.ini')
        with open(path, 'w') as f:
            f.write('[btcpay]\n'
                    'host = https://btcpay.exemplo/\n'
                    'store_id = loja1\n'
                    f'api_token = {api_token}\n'
                    'currency = BRL\n'
                    'crypto_code = BTC\n' + extra)
        return path


class TestClassificacaoDeFalhas(BaseBTCPay):
    def _post(self, **kwargs):
        return mock.patch.object(atm_core.requests, 'post', **kwargs)

    def test_sem_conexao_e_nao_transmitido(self):
        with self._post(side_effect=requests.ConnectionError('rede caiu')):
            with self.assertRaises(atm_core.PaymentNotBroadcast):
                atm_core.send_onchain_payment(100, ENDERECO, 500000)

    def test_timeout_e_incerto(self):
        """O servidor pode ter recebido e transmitido antes de estourar o prazo."""
        with self._post(side_effect=requests.Timeout('estourou')):
            with self.assertRaises(atm_core.PaymentUncertain):
                atm_core.send_onchain_payment(100, ENDERECO, 500000)

    def test_erro_de_rede_generico_e_incerto(self):
        with self._post(side_effect=requests.RequestException('???')):
            with self.assertRaises(atm_core.PaymentUncertain):
                atm_core.send_onchain_payment(100, ENDERECO, 500000)

    def test_4xx_e_nao_transmitido(self):
        for status in (400, 401, 404, 422):
            with self.subTest(status):
                with self._post(return_value=resposta(status, text='rejeitado')):
                    with self.assertRaises(atm_core.PaymentNotBroadcast):
                        atm_core.send_onchain_payment(100, ENDERECO, 500000)

    def test_5xx_e_incerto(self):
        for status in (500, 502, 503):
            with self.subTest(status):
                with self._post(return_value=resposta(status, text='boom')):
                    with self.assertRaises(atm_core.PaymentUncertain):
                        atm_core.send_onchain_payment(100, ENDERECO, 500000)

    def test_2xx_conclui(self):
        for status in (200, 201, 202):
            with self.subTest(status):
                with self._post(return_value=resposta(
                        status, {'transactionHash': 'abc123'})):
                    self.assertEqual(
                        atm_core.send_onchain_payment(100, ENDERECO, 500000),
                        'abc123')

    def test_corpo_ilegivel_apos_2xx_nao_levanta(self):
        """Depois de um 2xx o dinheiro já saiu. Se um erro de parsing vazasse,
        a GUI trataria como falha e reenfileiraria — gasto duplo."""
        with self._post(return_value=resposta(200, payload=None)):
            txid = atm_core.send_onchain_payment(100, ENDERECO, 500000)
        self.assertEqual(txid, 'desconhecido')

    def test_falha_do_telegram_nao_afeta_o_pagamento(self):
        """A notificação é acessória e roda DEPOIS de o Bitcoin sair. Se a
        exceção vazasse, um pagamento concluído viraria 'falha incerta' na
        tela — e o operador procuraria um problema que não existe."""
        self.telegram.side_effect = RuntimeError('telegram fora do ar')
        with self._post(return_value=resposta(200, {'transactionHash': 'ok'})):
            self.assertEqual(
                atm_core.send_onchain_payment(100, ENDERECO, 500000), 'ok')

    def test_notificacao_traz_valor_destino_e_txid(self):
        with self._post(return_value=resposta(200, {'transactionHash': 'abc123'})):
            atm_core.send_onchain_payment(100, ENDERECO, 50000)
        msg = self.telegram.call_args.kwargs['messages'][0]
        self.assertIn('R$ 100.00', msg)
        self.assertIn('0.00200000', msg)
        self.assertIn('abc123', msg)
        self.assertIn(ENDERECO[:30], msg)


class TestContratoOnchain(BaseBTCPay):
    """Fixa o contrato do Greenfield corrigido em a3020e9 — o path antigo
    (/on-chain/{crypto}/transactions) devolvia 404 e nenhum pagamento saía."""

    def enviar(self):
        with mock.patch.object(atm_core.requests, 'post',
                               return_value=resposta(200, {'transactionHash': 'x'})) as post:
            atm_core.send_onchain_payment(100, ENDERECO, 50000)
        return post.call_args

    def test_url_usa_payment_methods_wallet(self):
        args, _ = self.enviar()
        self.assertEqual(
            args[0],
            'https://btcpay.exemplo/api/v1/stores/loja1/'
            'payment-methods/BTC-CHAIN/wallet/transactions')

    def test_barra_final_do_host_e_removida(self):
        args, _ = self.enviar()
        self.assertNotIn('//api', args[0].replace('https://', ''))

    def test_payload_envia_valor_convertido_como_string(self):
        _, kwargs = self.enviar()
        destino = kwargs['json']['destinations'][0]
        self.assertEqual(destino['destination'], ENDERECO)
        self.assertEqual(destino['amount'], '0.00200000')
        self.assertIs(destino['subtractFromAmount'], False)
        self.assertIs(kwargs['json']['noChange'], False)

    def test_feerate_e_omitido(self):
        """Enviar feerate=null causa 422 no BTCPay; a ausência deixa o servidor
        estimar a taxa."""
        _, kwargs = self.enviar()
        self.assertNotIn('feerate', kwargs['json'])

    def test_token_descriptografado_vai_no_header(self):
        _, kwargs = self.enviar()
        self.assertEqual(kwargs['headers']['Authorization'],
                         f'token {self.API_TOKEN}')

    def test_payment_method_pode_ser_sobrescrito_pela_config(self):
        self.escrever_config(api_token=self.cifrado,
                             extra='onchain_payment_method = BTC\n')
        args, _ = self.enviar()
        self.assertIn('/payment-methods/BTC/wallet/', args[0])

    def test_tem_timeout(self):
        """Sem timeout, uma conexão pendurada congelaria a thread de trabalho
        para sempre e o ATM ficaria travado em 'Processando pagamento'."""
        _, kwargs = self.enviar()
        self.assertTrue(kwargs['timeout'])


class TestContratoLightning(BaseBTCPay):
    def test_invoice_bolt11_vai_direto(self):
        invoice = support.make_invoice()
        with mock.patch.object(atm_core.requests, 'post',
                               return_value=resposta(200, {'status': 'Complete',
                                                           'paymentHash': 'h1'})) as post:
            got = atm_core.send_lightning_payment(100, invoice, 50000)
        args, kwargs = post.call_args
        self.assertEqual(
            args[0],
            'https://btcpay.exemplo/api/v1/stores/loja1/lightning/BTC/invoices/pay')
        self.assertEqual(kwargs['json'], {'BOLT11': invoice})
        self.assertEqual(got, 'h1')

    def test_status_failed_e_nao_transmitido(self):
        """Pagamento Lightning é atômico: 'Failed' significa que nenhum fundo
        saiu, então reenfileirar é seguro."""
        with mock.patch.object(atm_core.requests, 'post',
                               return_value=resposta(200, {'status': 'Failed'})):
            with self.assertRaises(atm_core.PaymentNotBroadcast):
                atm_core.send_lightning_payment(100, support.make_invoice(), 50000)

    def test_status_pending_e_tratado_como_enviado(self):
        """A invoice é de uso único: reenviar o mesmo BOLT11 não pagaria duas
        vezes, mas 'Pending' não é falha e não pode voltar para a fila."""
        with mock.patch.object(atm_core.requests, 'post',
                               return_value=resposta(202, {'status': 'Pending',
                                                           'paymentHash': 'h2'})):
            self.assertEqual(
                atm_core.send_lightning_payment(100, support.make_invoice(), 50000),
                'h2')

    def test_corpo_ilegivel_nao_levanta(self):
        invoice = support.make_invoice()
        with mock.patch.object(atm_core.requests, 'post',
                               return_value=resposta(200, payload=None)):
            got = atm_core.send_lightning_payment(100, invoice, 50000)
        self.assertEqual(got, invoice[:20])

    def test_lightning_address_e_resolvido_para_invoice(self):
        invoice = support.make_invoice()
        with mock.patch.object(atm_core, '_resolve_lightning_address',
                               return_value=invoice) as resolver:
            with mock.patch.object(atm_core.requests, 'post',
                                   return_value=resposta(200, {'status': 'Complete',
                                                               'paymentHash': 'h'})) as post:
                atm_core.send_lightning_payment(100, 'joao@dominio.com', 50000)
        resolver.assert_called_once_with('joao@dominio.com', Decimal('0.00200000'))
        self.assertEqual(post.call_args.kwargs['json'], {'BOLT11': invoice})

    def test_falha_na_resolucao_nao_chega_a_pagar(self):
        """A resolução LNURL acontece toda ANTES do pagamento; por isso suas
        falhas são seguras para reenfileirar."""
        with mock.patch.object(atm_core, '_resolve_lightning_address',
                               side_effect=atm_core.PaymentNotBroadcast('domínio fora')):
            with mock.patch.object(atm_core.requests, 'post',
                                   side_effect=AssertionError('não deveria pagar')) as post:
                with self.assertRaises(atm_core.PaymentNotBroadcast):
                    atm_core.send_lightning_payment(100, 'joao@dominio.com', 50000)
        post.assert_not_called()

    def test_notificacao_traz_o_endereco_original_e_nao_a_invoice(self):
        """O operador precisa reconhecer o destino no alerta; uma invoice
        truncada em 30 caracteres não diz nada."""
        with mock.patch.object(atm_core, '_resolve_lightning_address',
                               return_value=support.make_invoice()):
            with mock.patch.object(atm_core.requests, 'post',
                                   return_value=resposta(200, {'status': 'Complete',
                                                               'paymentHash': 'h'})):
                atm_core.send_lightning_payment(100, 'joao@dominio.com', 50000)
        self.assertIn('joao@dominio.com',
                      self.telegram.call_args.kwargs['messages'][0])


class TestErroAntesDoBroadcast(BaseBTCPay):
    """Falhas de preparação (config.ini, /etc/atm/key, token Fernet).

    Tudo isso roda ANTES do POST, então prova que nada foi transmitido — cada
    teste confirma as duas metades: a rede nunca foi tocada E o erro chega
    classificado como PaymentNotBroadcast, para que a transação seja
    preservada na fila.

    Antes da correção (_not_broadcast_on_error em atm_core), essas falhas
    escapavam como FileNotFoundError/InvalidToken/KeyError e o chamador as
    tratava como ambíguas: a GUI não enfileirava e a fila descartava a
    transação. O cliente pagava em dinheiro e ficava sem nada, por um erro de
    configuração que nem chegou a tentar pagar. O comentário no topo de
    scripts/generate_key.py descrevia exatamente esse defeito."""

    def _sem_rede(self):
        return mock.patch.object(
            atm_core.requests, 'post',
            side_effect=AssertionError('não deveria chegar à rede'))

    def test_chave_fernet_ausente(self):
        """Cenário real: /etc/atm/key não montado depois de um boot."""
        os.remove(self.key_path)
        with self._sem_rede() as post:
            with self.assertRaises(atm_core.PaymentNotBroadcast):
                atm_core.send_onchain_payment(100, ENDERECO, 500000)
        post.assert_not_called()

    def test_chave_rotacionada_nao_decifra_o_token(self):
        with open(self.key_path, 'wb') as f:
            f.write(Fernet.generate_key())
        with self._sem_rede() as post:
            with self.assertRaises(atm_core.PaymentNotBroadcast):
                atm_core.send_onchain_payment(100, ENDERECO, 500000)
        post.assert_not_called()

    def test_config_sem_a_secao_btcpay(self):
        with open(self.config_path, 'w') as f:
            f.write('[outra_secao]\nx = 1\n')
        with self._sem_rede() as post:
            with self.assertRaises(atm_core.PaymentNotBroadcast):
                atm_core.send_onchain_payment(100, ENDERECO, 500000)
        post.assert_not_called()

    def test_lightning_tem_a_mesma_protecao(self):
        os.remove(self.key_path)
        with self._sem_rede() as post:
            with self.assertRaises(atm_core.PaymentNotBroadcast):
                atm_core.send_lightning_payment(100, support.make_invoice(), 500000)
        post.assert_not_called()

    def test_falha_ambigua_nao_e_rebaixada(self):
        """A proteção não pode engolir uma classificação já feita: rebaixar um
        PaymentUncertain para PaymentNotBroadcast faria a transação voltar
        para a fila e ser paga duas vezes."""
        with mock.patch.object(atm_core.requests, 'post',
                               side_effect=requests.Timeout('estourou')):
            with self.assertRaises(atm_core.PaymentUncertain):
                atm_core.send_onchain_payment(100, ENDERECO, 500000)


class TestRecibo(unittest.TestCase):
    def test_falha_da_impressora_nao_levanta(self):
        """O recibo é o último passo, depois de o Bitcoin já ter saído. Uma
        exceção aqui viraria 'falha incerta' num pagamento bem-sucedido."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'config.ini')
            with open(path, 'w') as f:
                f.write('[hardware]\nprinter_usb = 0416:5011\n')
            with mock.patch.object(atm_core, '_CONFIG_PATH', path):
                atm_core.print_receipt(100, Decimal('0.002'), ENDERECO, 'txid')


if __name__ == '__main__':
    unittest.main()
