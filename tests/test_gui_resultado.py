"""Desfecho do pagamento na GUI: o que acontece com o dinheiro quando o envio
falha.

Esta é a única parte da interface que decide destino de dinheiro. A regra que
os testes fixam:

  PaymentNotBroadcast -> enfileira (o cliente recebe depois)
  qualquer outra coisa -> NÃO enfileira (pode já ter sido pago; enfileirar
                          arriscaria pagar duas vezes)

O segundo caso está certo para timeouts e 5xx. Está errado para erros que
nunca chegaram à rede — ver TestErroDeConfiguracaoNaGui, no fim do arquivo.

Os widgets Qt são interceptados com mock, então os testes valem tanto com o
PyQt5 real (no Raspberry Pi) quanto com o stub da máquina de desenvolvimento.
"""

import logging
import os
import tempfile
import unittest
from unittest import mock

import support

import atm_core
import atm_gui

DESTINO = 'bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4'


class BaseDesfecho(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(atm_gui, 'QMessageBox')
        self.dialogo = p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(atm_gui, 'enqueue_transaction')
        self.enfileirar = p.start()
        self.addCleanup(p.stop)

    def janela_em_pagamento(self, amount=150):
        win = support.make_window()
        win.amount_brl = amount
        win.destination = DESTINO
        win.payment_type = 'onchain'
        win._payment_in_flight = True
        win._pending = (amount, DESTINO, 'onchain')
        return win


class TestSucesso(BaseDesfecho):
    def test_volta_ao_estado_ocioso_e_mostra_o_txid(self):
        win = self.janela_em_pagamento()
        win._on_payment_result('abc123def456')
        self.assertFalse(win._payment_in_flight)
        self.assertIsNone(win.amount_brl)
        self.assertIsNone(win.destination)
        self.assertIn('abc123def4', win.status_label.text)

    def test_nao_enfileira_nada(self):
        win = self.janela_em_pagamento()
        win._on_payment_result('abc')
        self.enfileirar.assert_not_called()


class TestFalhaComprovadamenteNaoTransmitida(BaseDesfecho):
    def test_enfileira_com_os_dados_da_transacao(self):
        win = self.janela_em_pagamento(amount=150)
        win._on_payment_error(atm_core.PaymentNotBroadcast('sem conexão'))
        self.enfileirar.assert_called_once_with(150, DESTINO, 'onchain', 500000.0)

    def test_usa_o_contexto_congelado_e_nao_o_estado_atual(self):
        """_pending guarda o que estava valendo quando o envio começou; o reset
        limpa a tela antes de o handler terminar."""
        win = self.janela_em_pagamento(amount=150)
        win.amount_brl = 999          # cliente mexeu na tela nesse meio-tempo
        win._on_payment_error(atm_core.PaymentNotBroadcast('sem conexão'))
        self.assertEqual(self.enfileirar.call_args.args[0], 150)

    def test_libera_a_maquina_para_o_proximo_cliente(self):
        win = self.janela_em_pagamento()
        win._on_payment_error(atm_core.PaymentNotBroadcast('sem conexão'))
        self.assertFalse(win._payment_in_flight)
        self.assertIsNone(win.amount_brl)

    def test_falha_ao_enfileirar_avisa_na_tela(self):
        self.enfileirar.side_effect = OSError('disco cheio')
        win = self.janela_em_pagamento()
        win._on_payment_error(atm_core.PaymentNotBroadcast('sem conexão'))
        self.dialogo.critical.assert_called_once()


class TestFalhaIncerta(BaseDesfecho):
    def test_nao_enfileira_para_evitar_gasto_duplo(self):
        """Timeout/5xx: o Bitcoin pode ter saído. Enfileirar pagaria de novo."""
        win = self.janela_em_pagamento()
        win._on_payment_error(atm_core.PaymentUncertain('timeout'))
        self.enfileirar.assert_not_called()

    def test_pede_verificacao_manual(self):
        win = self.janela_em_pagamento()
        win._on_payment_error(atm_core.PaymentUncertain('timeout'))
        self.dialogo.critical.assert_called_once()
        self.assertIn('carteira', win.status_label.text.lower())


class TestRegistroParaReconciliacao(BaseDesfecho):
    """ACHADO 4 DA REVISÃO — falhas esperadas até a correção.

    Nos dois cenários abaixo o cliente pagou e pode ter ficado sem nada, e a
    única saída é uma caixa de diálogo — num quiosque em tela cheia, sem
    ninguém na frente. Não há log com valor, destino e método, justo nos casos
    em que a mensagem manda o operador conferir manualmente.

    Correção prevista: logging.critical com a transação completa antes de
    exibir o diálogo."""

    @unittest.expectedFailure
    def test_falha_incerta_deve_ser_registrada(self):
        win = self.janela_em_pagamento(amount=150)
        with self.assertLogs(level=logging.CRITICAL) as cm:
            win._on_payment_error(atm_core.PaymentUncertain('timeout'))
        self.assertIn('150', ' '.join(cm.output))
        self.assertIn(DESTINO, ' '.join(cm.output))

    @unittest.expectedFailure
    def test_falha_ao_enfileirar_deve_ser_registrada(self):
        """Pior caso do sistema: não enviou E não conseguiu enfileirar. Sem
        log, não sobra nenhum registro do que o cliente pagou."""
        self.enfileirar.side_effect = OSError('disco cheio')
        win = self.janela_em_pagamento(amount=150)
        with self.assertLogs(level=logging.CRITICAL) as cm:
            win._on_payment_error(atm_core.PaymentNotBroadcast('sem conexão'))
        self.assertIn('150', ' '.join(cm.output))


class TestErroDeConfiguracaoNaGui(BaseDesfecho):
    """Cadeia completa, do worker ao handler.

    _execute_payment roda na thread de trabalho; a exceção que ele levanta é o
    que decide se o dinheiro do cliente é preservado. Um config.ini ilegível
    prova que nada foi transmitido (o mock de rede confirma), chega à GUI como
    PaymentNotBroadcast e a transação é enfileirada.

    Antes da correção do achado 1, escapava como exceção crua, caía no ramo
    genérico do handler e a transação era perdida."""

    def executar_com_config_quebrada(self):
        """Roda _execute_payment como o worker roda e devolve a exceção."""
        inexistente = os.path.join(tempfile.gettempdir(), 'config-inexistente.ini')
        with mock.patch.object(atm_core, '_CONFIG_PATH', inexistente):
            with mock.patch.object(
                    atm_core.requests, 'post',
                    side_effect=AssertionError('não deveria chegar à rede')) as post:
                with self.assertRaises(Exception) as ctx:
                    atm_gui._execute_payment(150, DESTINO, 'onchain', 500000.0, False)
        post.assert_not_called()
        return ctx.exception

    def test_erro_generico_nao_enfileira(self):
        """O ramo conservador continua valendo para o que resta de
        desconhecido — erros DEPOIS de o pagamento partir, quando o Bitcoin
        pode já ter saído."""
        win = self.janela_em_pagamento()
        win._on_payment_error(RuntimeError('qualquer coisa'))
        self.enfileirar.assert_not_called()

    def test_config_ilegivel_preserva_a_transacao(self):
        exc = self.executar_com_config_quebrada()
        win = self.janela_em_pagamento(amount=150)
        win._on_payment_error(exc)
        self.enfileirar.assert_called_once_with(150, DESTINO, 'onchain', 500000.0)


if __name__ == '__main__':
    unittest.main()
