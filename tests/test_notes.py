"""Leitura, enquadramento e crédito de cédulas.

É o ponto do sistema onde dinheiro FÍSICO entra: um erro aqui não pode ser
desfeito por software — ou credita valor que não existe, ou engole uma nota
que o cliente já colocou na máquina. O histórico do repositório mostra três
correções seguidas nesta mesma função; estes testes existem para que a quarta
não passe despercebida.
"""

import logging
import unittest

import support


def frame(valor):
    """Um quadro de cédula: NOTE_FRAME_BYTES bytes, big-endian."""
    return valor.to_bytes(2, 'big')


class TestEnquadramento(unittest.TestCase):
    """_parse_notes: interpretação do buffer serial bruto."""

    def parse(self, data):
        win = support.make_window()
        return win._parse_notes(data)

    def test_uma_cedula(self):
        self.assertEqual(self.parse(frame(50)), [50])

    def test_duas_cedulas_na_mesma_janela_de_leitura(self):
        """Regressão do commit d0efff9: com um único int.from_bytes sobre o
        buffer inteiro, este caso virava 0x00320064 = 3.276.900 — descartado
        pela whitelist, engolindo AS DUAS notas."""
        self.assertEqual(self.parse(frame(50) + frame(100)), [50, 100])

    def test_tres_cedulas_na_mesma_janela(self):
        self.assertEqual(self.parse(frame(20) + frame(20) + frame(200)),
                         [20, 20, 200])

    def test_todas_as_denominacoes_de_real(self):
        for valor in (2, 5, 10, 20, 50, 100, 200):
            with self.subTest(f'R${valor}'):
                self.assertEqual(self.parse(frame(valor)), [valor])

    def test_quadro_invalido_nao_contamina_os_validos(self):
        """R$3 não existe; a nota real de R$50 no mesmo buffer ainda é creditada."""
        self.assertEqual(self.parse(frame(50) + frame(3) + frame(20)), [50, 20])

    def test_descarta_denominacao_inexistente(self):
        for valor in (0, 1, 3, 500, 65535):
            with self.subTest(valor):
                self.assertEqual(self.parse(frame(valor)), [])

    def test_descarta_buffer_desalinhado(self):
        """Ruído elétrico com número ímpar de bytes não pode ser interpretado."""
        self.assertEqual(self.parse(b'\xff'), [])
        self.assertEqual(self.parse(b'\xff\x00\x32'), [])

    def test_buffer_vazio(self):
        self.assertEqual(self.parse(b''), [])


class TestFramePartido(unittest.TestCase):
    """ACHADO 2 DA REVISÃO — falha esperada até a correção.

    A leitura serial não garante que os 2 bytes de um quadro cheguem no mesmo
    poll de 1 segundo. Se a janela cair no meio do quadro, cada metade chega
    com comprimento ímpar e as duas são descartadas: a cédula está dentro da
    máquina e o cliente não recebe crédito nenhum.

    Correção prevista: manter um buffer residual entre polls, consumindo
    quadros completos e guardando o byte que sobrar.

    Ao corrigir, remover o expectedFailure (o unittest acusa 'unexpected
    success' e o run falha, então não há como esquecer)."""

    @unittest.expectedFailure
    def test_cedula_de_50_partida_entre_dois_polls(self):
        win = support.make_window(polls=[b'\x00', b'\x32'])
        win.check_note()
        win.check_note()
        self.assertEqual(win.amount_brl, 50)

    @unittest.expectedFailure
    def test_cedula_de_200_partida_entre_dois_polls(self):
        win = support.make_window(polls=[b'\x00', b'\xc8'])
        win.check_note()
        win.check_note()
        self.assertEqual(win.amount_brl, 200)


class TestCredito(unittest.TestCase):
    """_credit_note: acumulação e teto por transação."""

    def test_acumula_multiplas_cedulas(self):
        win = support.make_window()
        for valor in (50, 100, 20):
            win._credit_note(valor)
        self.assertEqual(win.amount_brl, 170)

    def test_primeira_cedula_habilita_os_botoes(self):
        win = support.make_window()
        self.assertFalse(win.onchain_button.enabled)
        win._credit_note(50)
        self.assertTrue(win.onchain_button.enabled)
        self.assertTrue(win.lightning_button.enabled)

    def test_aceita_cedula_depois_de_escolher_o_metodo(self):
        """Continuar inserindo notas após escolher on-chain/Lightning deve
        somar, e não reabrir a escolha do método."""
        win = support.make_window(payment_type='onchain')
        win._credit_note(50)
        win._credit_note(20)
        self.assertEqual(win.amount_brl, 70)
        self.assertFalse(win.onchain_button.enabled)

    def test_teto_recusa_cedula_seguinte(self):
        win = support.make_window(max_transaction_brl=100)
        win._credit_note(100)
        win._credit_note(50)
        self.assertEqual(win.amount_brl, 100)

    def test_teto_pode_ser_excedido_por_uma_unica_cedula(self):
        """Fixa a limitação conhecida: a nota já está dentro da máquina quando
        o valor é lido, então a que cruza o teto é creditada. Só a SEGUINTE é
        recusada. Só um inibidor de hardware resolve de fato."""
        win = support.make_window(max_transaction_brl=100)
        win._credit_note(200)
        self.assertEqual(win.amount_brl, 200)

    def test_cedula_recusada_gera_log_critico_para_reembolso(self):
        """A nota recusada ficou fisicamente na máquina: sem log CRÍTICO com o
        valor, o operador não tem como reembolsar o cliente."""
        win = support.make_window(max_transaction_brl=100)
        win._credit_note(100)
        with self.assertLogs(level=logging.CRITICAL) as cm:
            win._credit_note(50)
        self.assertIn('50', cm.output[0])

    def test_aviso_visivel_ao_atingir_o_teto(self):
        win = support.make_window(max_transaction_brl=100)
        win._credit_note(100)
        self.assertIn('100', win.instruction_label.text)


class TestLeituraSerial(unittest.TestCase):
    """check_note: drenagem do buffer e resiliência do quiosque."""

    def test_credita_a_partir_do_buffer(self):
        win = support.make_window(polls=[frame(50) + frame(20)])
        win.check_note()
        self.assertEqual(win.amount_brl, 70)

    def test_buffer_e_drenado_durante_o_pagamento(self):
        """Bytes que chegam durante o envio precisam ser CONSUMIDOS, senão
        seriam creditados como 'nota fantasma' logo após o reset."""
        win = support.make_window(polls=[frame(50)], _payment_in_flight=True)
        win.check_note()
        self.assertEqual(win.note_reader.polls, [])
        self.assertIsNone(win.amount_brl)

    def test_falha_do_noteiro_nao_derruba_o_processo(self):
        """Uma exceção dentro do slot de QTimer aborta o processo (qFatal) e,
        com systemd Restart=always, vira crash-loop."""
        class NoteiroQuebrado:
            @property
            def in_waiting(self):
                raise OSError('dispositivo desconectado')

        win = support.make_window()
        win.note_reader = NoteiroQuebrado()
        win.check_note()  # não pode levantar
        self.assertIsNone(win.amount_brl)


class TestNotaEngolidaDuranteOPagamento(unittest.TestCase):
    """ACHADO 3 DA REVISÃO — falha esperada até a correção.

    Descartar os bytes lidos durante o pagamento está certo (evita nota
    fantasma), mas hoje isso acontece sem UMA LINHA de log. A cédula está
    dentro da máquina e não existe registro para reembolsar o cliente —
    diferente do caminho do teto, que registra em nível crítico.

    Correção prevista: registrar o valor em nível CRÍTICO antes de descartar,
    e inibir o noteiro por hardware durante o envio."""

    @unittest.expectedFailure
    def test_nota_descartada_deve_gerar_log_critico(self):
        win = support.make_window(polls=[frame(50)], _payment_in_flight=True)
        with self.assertLogs(level=logging.CRITICAL):
            win.check_note()


if __name__ == '__main__':
    unittest.main()
