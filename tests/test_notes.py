"""Leitura, enquadramento e crédito de cédulas.

É o ponto do sistema onde dinheiro FÍSICO entra: um erro aqui não pode ser
desfeito por software — ou credita valor que não existe, ou engole uma nota
que o cliente já colocou na máquina. O histórico do repositório mostra três
correções seguidas nesta mesma função; estes testes existem para que a quarta
não passe despercebida.
"""

import logging
import random
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

    def test_byte_solto_nao_credita_nada(self):
        """Um byte sozinho não fecha quadro: fica guardado, sem creditar."""
        self.assertEqual(self.parse(b'\xff'), [])

    def test_buffer_vazio(self):
        self.assertEqual(self.parse(b''), [])


class TestBufferResidual(unittest.TestCase):
    """Quadro partido entre leituras e ressincronização do fluxo.

    A leitura serial não garante que os 2 bytes de um quadro cheguem no mesmo
    poll de 1 segundo. Antes da correção, cada metade chegava com comprimento
    ímpar e as duas eram descartadas: a cédula estava dentro da máquina e o
    cliente não recebia crédito nenhum."""

    def test_cedula_de_50_partida_entre_dois_polls(self):
        win = support.make_window(polls=[b'\x00', b'\x32'])
        win.check_note()
        self.assertIsNone(win.amount_brl)      # metade guardada, nada creditado
        win.check_note()
        self.assertEqual(win.amount_brl, 50)

    def test_cedula_de_200_partida_entre_dois_polls(self):
        win = support.make_window(polls=[b'\x00', b'\xc8'])
        win.check_note()
        win.check_note()
        self.assertEqual(win.amount_brl, 200)

    def test_quadro_e_meio_no_mesmo_poll(self):
        """Uma nota completa e o começo da seguinte na mesma janela."""
        win = support.make_window(polls=[frame(50) + b'\x00', b'\x64'])
        win.check_note()
        self.assertEqual(win.amount_brl, 50)
        win.check_note()
        self.assertEqual(win.amount_brl, 150)

    def test_sobra_no_maximo_um_quadro_incompleto(self):
        win = support.make_window(polls=[frame(50) + b'\x00'])
        win.check_note()
        self.assertEqual(win._note_buf, b'\x00')

    def test_ruido_colado_na_nota_ainda_credita_a_nota(self):
        """Ressincronização: o byte de ruído é descartado sozinho e o quadro
        real, que ficou deslocado atrás dele, é lido corretamente."""
        win = support.make_window(polls=[b'\xff' + frame(50)])
        win.check_note()
        self.assertEqual(win.amount_brl, 50)

    def test_ruido_nao_desalinha_as_cedulas_seguintes(self):
        """O risco da correção: sem ressincronizar, um único byte de ruído
        deslocaria o fluxo para sempre e toda cédula posterior seria lida
        metade com metade, rejeitada e engolida em silêncio."""
        win = support.make_window(polls=[b'\xff', frame(50), frame(100), frame(20)])
        for _ in range(4):
            win.check_note()
        self.assertEqual(win.amount_brl, 170)

    def test_leitura_deslocada_nunca_inventa_uma_nota(self):
        """Toda denominação válida tem byte alto 0x00, então um par deslocado
        (byte baixo do quadro anterior + 0x00) nunca cai na whitelist."""
        win = support.make_window()
        for valor in (2, 5, 10, 20, 50, 100, 200):
            with self.subTest(f'R${valor}'):
                deslocado = int.from_bytes(frame(valor)[1:] + b'\x00', 'big')
                self.assertNotIn(deslocado, win.VALID_DENOMINATIONS)

    def test_fuzz_nunca_credita_mais_do_que_foi_inserido(self):
        """Propriedade central do parser: ruído pode CUSTAR uma cédula, mas
        nunca pode CRIAR dinheiro. Fluxos aleatórios de quadros válidos
        misturados com ruído, entregues em pedaços de tamanho aleatório (que é
        como a serial de fato entrega), nunca creditam mais que o inserido.

        O ruído exclui 0x00 de propósito: dois bytes de lixo iguais a 0x00 0x32
        SÃO um quadro válido de R$50, e nenhuma whitelist distingue isso de uma
        nota real — é limitação do protocolo de 2 bytes, não do parser."""
        rng = random.Random(20260811)
        denominacoes = sorted(support.make_window().VALID_DENOMINATIONS)
        for caso in range(300):
            inserido, fluxo = 0, b''
            for _ in range(rng.randint(1, 6)):
                if rng.random() < 0.25:
                    fluxo += bytes([rng.randrange(1, 256)])
                else:
                    valor = rng.choice(denominacoes)
                    inserido += valor
                    fluxo += frame(valor)

            win = support.make_window(max_transaction_brl=10 ** 9)
            pos = 0
            while pos < len(fluxo):
                pedaco = fluxo[pos:pos + rng.randint(1, 4)]
                pos += len(pedaco)
                for valor in win._parse_notes(pedaco):
                    win._credit_note(valor)

            with self.subTest(caso=caso, fluxo=fluxo.hex()):
                self.assertLessEqual(win.amount_brl or 0, inserido)

    def test_fuzz_fluxo_limpo_credita_exatamente_o_inserido(self):
        """A outra direção: num fluxo sem ruído, NENHUMA cédula pode se perder,
        não importa onde a leitura serial corte os pedaços. É o achado 2 na
        forma geral — antes da correção, qualquer corte em posição ímpar
        descartava o buffer e engolia as notas."""
        rng = random.Random(7042026)
        denominacoes = sorted(support.make_window().VALID_DENOMINATIONS)
        for caso in range(300):
            notas = [rng.choice(denominacoes) for _ in range(rng.randint(1, 6))]
            fluxo = b''.join(frame(v) for v in notas)

            win = support.make_window(max_transaction_brl=10 ** 9)
            pos = 0
            while pos < len(fluxo):
                pedaco = fluxo[pos:pos + rng.randint(1, 4)]
                pos += len(pedaco)
                for valor in win._parse_notes(pedaco):
                    win._credit_note(valor)

            with self.subTest(caso=caso, notas=notas):
                self.assertEqual(win.amount_brl, sum(notas))
                self.assertEqual(win._note_buf, b'')

    def test_reset_descarta_quadro_incompleto(self):
        """Meia nota não pode atravessar para o próximo cliente: combinada com
        os bytes dele, corromperia a leitura da primeira cédula."""
        win = support.make_window(polls=[b'\x00'])
        win.check_note()
        self.assertEqual(win._note_buf, b'\x00')
        win.reset()
        self.assertEqual(win._note_buf, b'')


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
