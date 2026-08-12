"""Fila offline: garantia de NO-MÁXIMO-UMA-VEZ.

O desenho é deliberadamente assimétrico: a transação sai da fila persistida
ANTES da tentativa de envio, então uma queda de energia no meio do envio perde
o REGISTRO (reconciliável pelos logs) em vez de arriscar reenviar Bitcoin já
transmitido. Só uma falha comprovadamente-não-transmitida volta para a fila.

Esses testes fixam essa assimetria, que é fácil de inverter sem perceber num
refactor — e cara de descobrir em produção.
"""

import json
import os
import tempfile
import time
import unittest
from decimal import Decimal
from unittest import mock

import support

import atm_core

DESTINO = 'bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4'


class BaseFila(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.queue_path = os.path.join(self.dir, 'atm', 'offline_queue.json')
        p = mock.patch.object(atm_core, '_QUEUE_PATH', self.queue_path)
        p.start()
        self.addCleanup(p.stop)

        self.enviados = []
        self.recibos = []
        p = mock.patch.object(atm_core, 'get_btc_rate', return_value=500000.0)
        self.get_rate = p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(atm_core, 'print_receipt',
                              side_effect=lambda *a: self.recibos.append(a))
        p.start()
        self.addCleanup(p.stop)

    def instalar_envio(self, onchain=None, lightning=None):
        def padrao(amount, dest, rate):
            self.enviados.append((amount, dest, rate))
            return 'txid-ok'

        for nome, fn in (('send_onchain_payment', onchain or padrao),
                         ('send_lightning_payment', lightning or padrao)):
            p = mock.patch.object(atm_core, nome, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)

    def ler_fila(self):
        if not os.path.exists(self.queue_path):
            return []
        with open(self.queue_path) as f:
            return json.load(f)

    def escrever_fila(self, itens):
        os.makedirs(os.path.dirname(self.queue_path), exist_ok=True)
        with open(self.queue_path, 'w') as f:
            json.dump(itens, f)

    def tx(self, amount=100, destino=DESTINO, tipo='onchain', rate=500000.0, idade=0):
        return {'amount_brl': amount, 'destination': destino,
                'payment_type': tipo, 'rate': rate,
                'timestamp': time.time() - idade}


class TestEnfileiramento(BaseFila):
    def test_persiste_todos_os_campos(self):
        atm_core.enqueue_transaction(100, DESTINO, 'onchain', 500000.0)
        (item,) = self.ler_fila()
        self.assertEqual(item['amount_brl'], 100)
        self.assertEqual(item['destination'], DESTINO)
        self.assertEqual(item['payment_type'], 'onchain')
        self.assertEqual(item['rate'], 500000.0)
        self.assertAlmostEqual(item['timestamp'], time.time(), delta=5)

    def test_preserva_ordem_de_chegada(self):
        for i in (1, 2, 3):
            atm_core.enqueue_transaction(i * 10, DESTINO, 'onchain', 500000.0)
        self.assertEqual([i['amount_brl'] for i in self.ler_fila()], [10, 20, 30])

    def test_escrita_atomica_nao_deixa_arquivo_temporario(self):
        """A gravação usa arquivo temporário + os.replace para que uma queda de
        energia no meio não corrompa o JSON — o que faria _load_queue devolver
        [] e descartar TODAS as pendências em silêncio."""
        atm_core.enqueue_transaction(100, DESTINO, 'onchain', 500000.0)
        self.assertFalse(os.path.exists(self.queue_path + '.tmp'))

    def test_json_corrompido_nao_derruba_o_processo(self):
        self.escrever_fila([])
        with open(self.queue_path, 'w') as f:
            f.write('{lixo binário]')
        self.assertEqual(atm_core._load_queue(), [])


class TestProcessamento(BaseFila):
    def test_sucesso_esvazia_a_fila_e_imprime_recibo(self):
        self.instalar_envio()
        self.escrever_fila([self.tx()])
        atm_core.process_offline_queue()
        self.assertEqual(self.ler_fila(), [])
        self.assertEqual(len(self.enviados), 1)
        (amount, btc, destino, txid), = self.recibos
        self.assertEqual(amount, 100)
        self.assertEqual(btc, Decimal('0.00020000'))
        self.assertEqual(txid, 'txid-ok')

    def test_transacao_sai_da_fila_antes_do_envio(self):
        """No-máximo-uma-vez: no instante do envio a transação já não está
        persistida, então um crash no meio não a reenvia no próximo boot."""
        vista = []

        def espiar(amount, dest, rate):
            vista.append(self.ler_fila())
            return 'txid'

        self.instalar_envio(onchain=espiar)
        self.escrever_fila([self.tx()])
        atm_core.process_offline_queue()
        self.assertEqual(vista, [[]])

    def test_fila_de_tres_drena_em_uma_passagem(self):
        self.instalar_envio()
        self.escrever_fila([self.tx(amount=10), self.tx(amount=20), self.tx(amount=30)])
        atm_core.process_offline_queue()
        self.assertEqual([e[0] for e in self.enviados], [10, 20, 30])
        self.assertEqual(self.ler_fila(), [])

    def test_lightning_usa_o_envio_lightning(self):
        self.instalar_envio()
        self.escrever_fila([self.tx(tipo='lightning', destino=support.make_invoice())])
        atm_core.process_offline_queue()
        self.assertEqual(atm_core.send_lightning_payment.call_count, 1)
        self.assertEqual(atm_core.send_onchain_payment.call_count, 0)

    def test_enfileiramento_durante_o_processamento_nao_se_perde(self):
        """A GUI pode enfileirar enquanto o worker drena a fila; o read-modify-
        write das duas pontas é serializado pelo lock."""
        def envio_que_enfileira(amount, dest, rate):
            atm_core.enqueue_transaction(999, DESTINO, 'onchain', rate)
            return 'txid'

        self.instalar_envio(onchain=envio_que_enfileira)
        self.escrever_fila([self.tx(amount=10)])
        atm_core.process_offline_queue()
        self.assertEqual([i['amount_brl'] for i in self.ler_fila()], [999])


class TestClassificacaoNaFila(BaseFila):
    def falha(self, exc):
        def _falha(amount, dest, rate):
            self.enviados.append((amount, dest, rate))
            raise exc
        return _falha

    def test_nao_transmitido_volta_para_a_fila(self):
        self.instalar_envio(onchain=self.falha(
            atm_core.PaymentNotBroadcast('sem conexão')))
        self.escrever_fila([self.tx()])
        atm_core.process_offline_queue()
        self.assertEqual(len(self.ler_fila()), 1)

    def test_nao_transmitido_nao_e_retentado_na_mesma_passagem(self):
        """Regressão do commit d0efff9: com `while True`, a transação recolocada
        na fila era repescada imediatamente — laço infinito martelando o BTCPay
        numa thread de background."""
        self.instalar_envio(onchain=self.falha(
            atm_core.PaymentNotBroadcast('endereço rejeitado')))
        self.escrever_fila([self.tx()])
        atm_core.process_offline_queue()
        self.assertEqual(len(self.enviados), 1)

    def test_incerto_e_descartado_para_evitar_gasto_duplo(self):
        """Timeout/5xx: o Bitcoin pode ter saído. Reenfileirar pagaria duas
        vezes; o operador confere a carteira pelos logs."""
        self.instalar_envio(onchain=self.falha(
            atm_core.PaymentUncertain('timeout')))
        self.escrever_fila([self.tx()])
        atm_core.process_offline_queue()
        self.assertEqual(self.ler_fila(), [])

    def test_excecao_inesperada_e_descartada(self):
        """FIXA O COMPORTAMENTO ATUAL. É conservador para erros pós-envio, mas
        hoje engole também os erros PRÉ-envio (achado 1: config/chave Fernet),
        que provadamente nada transmitiram e deveriam voltar para a fila.
        Ao corrigir a classificação em atm_core, este teste deve passar a
        exigir o reenfileiramento."""
        self.instalar_envio(onchain=self.falha(KeyError('btcpay')))
        self.escrever_fila([self.tx()])
        atm_core.process_offline_queue()
        self.assertEqual(self.ler_fila(), [])

    def test_recolocada_no_fim_preservando_as_demais(self):
        self.instalar_envio(onchain=self.falha(
            atm_core.PaymentNotBroadcast('falhou')))
        self.escrever_fila([self.tx(amount=10), self.tx(amount=20)])
        atm_core.process_offline_queue()
        self.assertEqual([i['amount_brl'] for i in self.ler_fila()], [10, 20])
        self.assertEqual(len(self.enviados), 2)


class TestCotacao(BaseFila):
    def test_sem_cotacao_adia_a_fila_inteira(self):
        self.instalar_envio()
        self.get_rate.return_value = None
        self.escrever_fila([self.tx(rate=None), self.tx(rate=None)])
        atm_core.process_offline_queue()
        self.assertEqual(self.enviados, [])
        self.assertEqual(len(self.ler_fila()), 2)

    def test_cotacao_recente_e_reaproveitada(self):
        self.instalar_envio()
        self.escrever_fila([self.tx(rate=111.0, idade=10)])
        atm_core.process_offline_queue()
        self.assertEqual(self.enviados[0][2], 111.0)
        self.get_rate.assert_not_called()

    def test_cotacao_velha_forca_busca_fresca(self):
        """Liquidar horas depois com o preço antigo enviaria BTC a mais se o
        preço tiver caído desde então."""
        self.instalar_envio()
        self.escrever_fila([self.tx(rate=111.0, idade=atm_core._QUEUE_RATE_MAX_AGE + 60)])
        atm_core.process_offline_queue()
        self.assertEqual(self.enviados[0][2], 500000.0)
        self.get_rate.assert_called_once()

    def test_sem_cotacao_salva_busca_fresca(self):
        self.instalar_envio()
        self.escrever_fila([self.tx(rate=None)])
        atm_core.process_offline_queue()
        self.assertEqual(self.enviados[0][2], 500000.0)


class TestRetentativaInfinita(BaseFila):
    """ACHADO 5 DA REVISÃO — falha esperada até a correção.

    Uma transação que falha de forma determinística (endereço que o BTCPay
    rejeita com 4xx) volta para a fila em toda passagem, a cada 5 minutos,
    para sempre. Não há contador de tentativas nem fila de descarte, então
    ninguém é avisado e o cliente nunca é reembolsado.

    Correção prevista: contar tentativas na própria transação e, ao estourar o
    limite, mover para uma fila de descarte com log crítico."""

    @unittest.expectedFailure
    def test_desiste_depois_de_algumas_tentativas(self):
        self.instalar_envio(onchain=lambda a, d, r: (_ for _ in ()).throw(
            atm_core.PaymentNotBroadcast('rejeitado 400: endereço inválido')))
        self.escrever_fila([self.tx()])
        for _ in range(10):
            atm_core.process_offline_queue()
        self.assertEqual(self.ler_fila(), [])


if __name__ == '__main__':
    unittest.main()
