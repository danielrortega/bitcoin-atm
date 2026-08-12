"""Conversão BRL -> BTC.

Regra do negócio: o ATM nunca pode enviar MAIS BTC do que o cliente pagou.
Isso depende de dois detalhes que um refactor descuidado quebra sem alarde —
aritmética decimal (não float) e arredondamento para baixo.
"""

import unittest
from decimal import Decimal

import support  # noqa: F401  — insere src/ no sys.path; precisa vir antes

import atm_core


class TestBrlParaBtc(unittest.TestCase):
    def test_conversao_exata(self):
        self.assertEqual(atm_core.brl_to_btc(100, 50000), Decimal('0.002'))

    def test_resultado_e_decimal_com_8_casas(self):
        got = atm_core.brl_to_btc(100, 50000)
        self.assertIsInstance(got, Decimal)
        self.assertEqual(got.as_tuple().exponent, -8)

    def test_arredonda_para_baixo_nunca_para_cima(self):
        """R$100 a R$300.000/BTC = 0,000333333... BTC. O nono decimal em diante
        é descartado, não arredondado — arredondar para cima enviaria satoshis
        que o cliente não pagou, em toda transação."""
        self.assertEqual(atm_core.brl_to_btc(100, 300000), Decimal('0.00033333'))

    def test_trunca_valor_proximo_do_arredondamento(self):
        """0,999999995 BTC arredondaria para 1,00000000 com ROUND_HALF_EVEN.
        Com ROUND_DOWN o valor fica em 0,99999999."""
        self.assertEqual(atm_core.brl_to_btc(Decimal('0.999999995'), 1),
                         Decimal('0.99999999'))

    def test_aceita_cotacao_float_sem_ruido_binario(self):
        """get_btc_rate devolve float. A conversão via str() evita que o ruído
        de representação binária vaze para o valor enviado."""
        self.assertEqual(atm_core.brl_to_btc(10, 0.1), Decimal('100'))

    def test_valor_menor_que_um_satoshi_vira_zero(self):
        """Fixa o comportamento atual: uma quantia pequena demais converte para
        0 BTC. Nada impede hoje um pagamento de 0 satoshi ser tentado."""
        self.assertEqual(atm_core.brl_to_btc(Decimal('0.000001'), 1000000),
                         Decimal('0'))

    def test_denominacoes_reais_de_cedula(self):
        rate = 500000
        for brl in (2, 5, 10, 20, 50, 100, 200):
            with self.subTest(f'R${brl}'):
                got = atm_core.brl_to_btc(brl, rate)
                self.assertEqual(got, (Decimal(brl) / Decimal(rate)).quantize(
                    Decimal('0.00000001')))
                self.assertLessEqual(got * Decimal(rate), Decimal(brl))


if __name__ == '__main__':
    unittest.main()
